"""Disposable wallets, synthetic public feeds; no external wallet or requests."""
from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from polybot2other.paired_paper import PairedPaperEngine, pair_quote, fee, DEFAULTS
from polybot2other.paired_paper_service import verify_rules, verified_result


def market(key='m', start=0):
    return {'round_id':key,'slug':key,'condition_id':key,'started_at':start,'ends_at':start+300000,
            'up_token':key+'u','down_token':key+'d'}


def rules():
    return {'tick_size':'.01','min_order_size':'5','fee_rate':'.07','fee_exponent':'1','evidence_hash':'test'}


def books(at,key='m',up='.49',down='.49',size='100'):
    return {s:{'token_id':key+suffix,'source_ms':at,'received_ms':at,
        'bids':[{'price':p,'size':size}], 'asks':[{'price':str(D(p)+D('.01')),'size':size}],
        'best_bid':float(p),'best_ask':float(D(p)+D('.01'))}
        for s,suffix,p in [('Up','u',up),('Down','d',down)]}


def print_(at,side='Up',price='.49',size='100',key='m',action='SELL',ident=None):
    return {'event_type':'last_trade_price','market':key,'asset_id':key+('u' if side=='Up' else 'd'),
            'timestamp':str(at),'side':action,'price':price,'size':size,'transaction_hash':ident or str(at)+side}


class PairEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'pair.sqlite3';self.e=PairedPaperEngine(self.path,at_ms=0)
        self.addCleanup(lambda:self.e.close())

    def tick(self,at,**kw):self.e.tick(at,market(),books(at,**kw),rules())

    def opened(self):
        self.tick(20000);self.tick(21900);self.tick(22000)
        self.assertEqual([o['status'] for o in self.e.orders(active=True)],['OPEN','OPEN'])

    def fill_both(self,at=23000):
        self.opened()
        self.e.tape(print_(at,size='1000'),at);self.e.tape(print_(at,'Down',size='1000'),at)

    def test_starts_paper_without_model_or_training_and_reserves_combined_five(self):
        self.tick(20000);s=self.e.snapshot(20000)
        self.assertEqual(s['mode'],'PAPER');self.assertFalse(s['live_enabled'])
        self.assertEqual(len(s['orders']),2);self.assertEqual(D(s['cash']),D(100))
        self.assertLessEqual(D(s['reserved']),D(5));self.assertGreater(D(s['reserved']),D('4.99'))

    def test_real_books_separate_and_pair_budget_minimum(self):
        p,q=pair_quote(books(0,up='.60',down='.39'),rules(),'5')
        self.assertLessEqual(sum(p.values()),D('.98'));self.assertLessEqual(q*sum(p.values()),5)
        self.assertEqual(p['Down'],D('.39'))
        with self.assertRaisesRegex(ValueError,'预算不足'):pair_quote(books(0),rules(),'3')

    def test_touching_or_buy_print_never_creates_passive_fill(self):
        self.opened();self.tick(23000,up='.48')
        self.e.tape(print_(23000,action='BUY',size='9999'),23000)
        self.assertEqual(self.e.snapshot(23000)['fills'],[])

    def test_queue_ahead_consumed_before_fill_and_partial_volume_capped(self):
        self.opened();self.e.tape(print_(23000,size='80'),23000)
        self.assertEqual(self.e.snapshot(23000)['fills'],[])
        self.e.tape(print_(23100,size='24'),23100)
        f=self.e.snapshot(23100)['fills'][0]
        self.assertEqual(D(f['quantity']),D(1));self.assertEqual(D(f['cash']),D('.49'))
        self.assertEqual(D(self.e.wallet()['cash']),D('99.51'))

    def test_duplicate_trade_no_double_cash_or_fill(self):
        self.opened();p=print_(23000,size='1000');self.e.tape(p,23000);cash=self.e.wallet()['cash']
        self.e.tape(p,23500);self.assertEqual(self.e.wallet()['cash'],cash)
        self.assertEqual(len(self.e.snapshot(23500)['fills']),1)

    def test_visible_queue_removal_is_not_fill_but_reduces_ahead_conservatively(self):
        self.opened();self.tick(23000,size='10')
        self.assertEqual(self.e.snapshot(23000)['fills'],[])
        self.e.tape(print_(23100,size='14'),23100)
        self.assertEqual(D(self.e.snapshot(23100)['fills'][0]['quantity']),1)

    def test_same_source_print_book_delta_cannot_double_count_queue(self):
        self.opened();self.tick(23000,size='1')
        self.e.tape(print_(23000,size='99'),23000)
        self.assertEqual(self.e.snapshot(23000)['fills'],[])

    def test_adverse_ask_through_is_accounted_at_original_price_not_favorable_new_ask(self):
        self.opened();self.tick(23000,up='.46',size='4')
        f=self.e.snapshot(23000)['fills'][0]
        self.assertEqual(f['source'],'ASK_THROUGH_CONSERVATIVE');self.assertEqual(D(f['quantity']),1)
        self.assertEqual(D(f['price']),D('.49'));self.assertEqual(D(self.e.wallet()['cash']),D('99.51'))
        self.tick(23100,up='.46',size='4')
        self.assertEqual(len(self.e.snapshot(23100)['fills']),1)

    def test_ask_through_after_effective_cancellation_does_not_backfill_order(self):
        self.opened();g=self.e.groups()[0]
        with self.e.tx():self.e.cancel(g,23000,'test')
        self.tick(25100,up='.46')
        self.assertEqual(self.e.snapshot(25100)['fills'],[])

    def test_no_fill_before_arrival_future_source_or_stale_print(self):
        self.tick(20000);self.e.tape(print_(21000,size='1000'),21000)
        self.tick(21900);self.tick(22000)
        self.e.tape(print_(24000,size='1000'),23000)
        self.e.tape(print_(23000,size='1000'),27000)
        self.assertEqual(self.e.snapshot(27000)['fills'],[])

    def test_maker_postonly_rejects_at_arrival_and_retains_cash(self):
        self.tick(20000);self.tick(21900,up='.47');self.tick(22000,up='.47')
        first=self.e.orders()[0];self.assertEqual(first['status'],'DONE')
        self.assertIn('post-only',first['terminal_reason']);self.assertEqual(D(self.e.wallet()['cash']),100)

    def test_no_future_book_used_for_activation(self):
        self.tick(20000)
        self.e.tick(22200,market(),books(22200),rules())
        self.assertIn('无有效新盘口',self.e.orders()[0]['terminal_reason'])

    def test_full_pair_confirm_then_merge_claim_only_once_and_conserve_cash(self):
        self.fill_both();g=self.e.groups()[0]
        self.assertEqual(D(g['paired']),0);self.assertEqual(D(self.e.wallet()['cash']),D('95.002'))
        self.tick(34999);self.assertEqual(D(self.e.wallet()['realized']),0)
        self.tick(35000);g=self.e.groups()[0]
        self.assertEqual(D(g['paired']),D('5.10'));self.assertEqual(D(g['claim']),D('5.10'))
        self.assertEqual(D(g['pnl']),D('.102'));self.assertEqual(D(self.e.snapshot(35000)['nav']),D('100.102'))
        self.tick(94999);self.assertEqual(D(self.e.wallet()['cash']),D('95.002'))
        self.tick(95000);self.tick(96000)
        self.assertEqual(D(self.e.wallet()['cash']),D('100.102'))
        self.assertEqual(D(self.e.wallet()['realized']),D('.102'))

    def test_cancel_latency_accepts_pre_cancel_late_print_and_no_post_cancel_fill(self):
        self.opened();g=self.e.groups()[0]
        with self.e.tx():self.e.cancel(g,23000,'test')
        self.tick(25000)
        self.e.tape(print_(24900,size='104'),25100)
        self.assertEqual(len(self.e.snapshot(25100)['fills']),1)
        before=self.e.wallet()['cash'];self.e.tape(print_(25100,size='999'),25200)
        self.assertEqual(before,self.e.wallet()['cash'])
        self.assertGreater(self.e.reserved(),0)

    def test_unpaired_small_tail_waits_final_never_invented_minimum_fill(self):
        self.opened();self.e.tape(print_(23000,size='104'),23000) # 1 share, below submit minimum
        self.tick(35000);self.tick(37000);self.tick(43000)
        g=self.e.groups()[0];self.assertEqual(g['phase'],'SMALL_TAIL')
        self.assertFalse(any(o['kind']=='EXIT' for o in self.e.orders()))
        self.e.official_result('m','Down',301000,{'verified':'fixture'})
        self.e.tick(301000,None,{},None)
        self.assertEqual(D(self.e.wallet()['cash']),D('99.51'))
        self.assertEqual(D(self.e.wallet()['realized']),D('-.49'))

    def test_unpaired_exit_uses_later_actual_depth_fees_and_partial_fill(self):
        self.opened();self.e.tape(print_(23000,size='1000'),23000)
        self.tick(35000);self.tick(37000);self.tick(43000,up='.47',down='.51')
        exits=[o for o in self.e.orders() if o['kind']=='EXIT'];self.assertEqual(len(exits),1)
        self.tick(44900,up='.47',down='.51',size='8');self.tick(45000,up='.47',down='.51',size='8')
        sells=[f for f in self.e.snapshot(45000)['fills'] if f['action']=='SELL']
        self.assertEqual(D(sells[0]['quantity']),2)
        self.assertEqual(D(sells[0]['fee']),fee('2','.47',rules()))
        before=D(self.e.wallet()['cash']);self.tick(57000,up='.47',down='.51')
        self.assertEqual(D(self.e.wallet()['cash']),before+D(sells[0]['cash']))
        self.assertLess(D(self.e.wallet()['realized']),0)

    def test_restart_cancels_quotes_but_keeps_known_fills_and_cash(self):
        self.opened();self.e.tape(print_(23000,size='1000'),23000);before=self.e.wallet()['cash']
        self.e.close();self.e=PairedPaperEngine(self.path,at_ms=24000)
        self.assertEqual(self.e.orders(active=True),[]);self.assertEqual(self.e.wallet()['cash'],before)
        self.e.tick(35000,None,{},None)
        self.assertEqual(self.e.snapshot(35000)['fills'][0]['status'],'CONFIRMED')
        self.assertFalse(self.e.groups()[0]['quality_ok'])

    def test_settings_cannot_enable_live_or_reset_cash(self):
        for payload in [{'live_enabled':True},{'initial_cash':1000},{'budget':'NaN'},{'max_markets':1.2}]:
            with self.subTest(payload=payload),self.assertRaises(ValueError):self.e.settings(payload,0)
        self.e.settings({'enabled':False,'budget':'6.00'},100)
        self.tick(20000);self.assertEqual(self.e.orders(),[])

    def test_stale_unpaired_valuation_is_unknown_not_cost_or_zero(self):
        self.opened();self.e.tape(print_(23000,size='1000'),23000)
        self.assertIsNone(self.e.nav(26001));self.assertIsNotNone(self.e.nav(24000))

    def test_market_ownership_single_group_and_no_cross_market_fill(self):
        self.opened();self.e.tape(print_(23000,key='other',size='1000'),23000)
        self.assertEqual(self.e.snapshot(23000)['fills'],[])
        self.fill_existing=1
        for at in [23000,24000,25000]:self.tick(at)
        self.assertEqual(len(self.e.groups()),1)

    def test_hedge_only_when_total_cost_and_cash_fit_then_merges(self):
        self.opened();self.e.tape(print_(23000,size='1000'),23000)
        self.tick(35000);self.tick(37000);self.tick(43000,down='.45')
        hedge=[o for o in self.e.orders() if o['kind']=='HEDGE'];self.assertEqual(len(hedge),1)
        self.tick(44900,down='.45');self.tick(45000,down='.45');self.tick(57000,down='.45')
        g=self.e.groups()[0];self.assertGreater(D(g['paired']),0);self.assertLessEqual(D(g['spent']),5)
        self.assertGreater(D(g['pnl']),0)

    def test_transaction_rolls_back_cash_inventory_and_print_once(self):
        self.opened();original=self.e.save_order
        def bad(o):
            if D(o['filled'])>0:raise OSError('disk fixture')
            original(o)
        self.e.save_order=bad
        with self.assertRaises(OSError):self.e.tape(print_(23000,size='1000'),23000)
        self.assertEqual(D(self.e.wallet()['cash']),100);self.assertEqual(self.e.snapshot(23000)['fills'],[])
        self.e.save_order=original;self.e.tape(print_(23000,size='1000'),23000)
        self.assertEqual(len(self.e.snapshot(23000)['fills']),1)

    def test_conflicting_final_result_preserves_ledger_and_marks_unknown(self):
        self.opened();self.e.tape(print_(23000,size='104'),23000)
        self.tick(35000);self.tick(37000);self.tick(43000)
        self.e.official_result('m','Down',301000,{'proof':'1'});self.e.tick(301000,None,{},None)
        cash=self.e.wallet()['cash'];self.e.official_result('m','Up',302000,{'proof':'2'})
        self.assertEqual(self.e.wallet()['cash'],cash);self.assertIsNone(self.e.nav(302000))

    def test_pause_cancels_quotes_but_known_fills_still_confirm(self):
        self.opened();self.e.tape(print_(23000,size='104'),23000)
        self.e.settings({'enabled':False},24000);self.tick(24000);self.tick(35000)
        self.assertEqual(self.e.snapshot(35000)['fills'][0]['status'],'CONFIRMED')
        self.assertEqual(self.e.last_code,'PAUSED')

    def test_global_paper_pause_preserves_independent_setting_and_resumes(self):
        self.e.tick(20000,market(),books(20000),rules(),entry_pause_reason='global pause')
        self.assertEqual(self.e.orders(),[]);self.assertTrue(self.e.config['enabled'])
        self.tick(20100);self.assertEqual(len(self.e.orders()),2)


class RulesTests(unittest.TestCase):
    def fixture(self):
        m=market();raw={'c':'m','v':'v1','t':[{'o':'Up','t':'mu'},{'o':'Down','t':'md'}],
            'ao':True,'fd':{'r':.07,'e':1,'to':True},'mts':'.01','mos':5}
        gamma={'conditionId':'m','slug':'m','version':'v1','outcomes':['Up','Down'],'clobTokenIds':['mu','md'],
            'acceptingOrders':True,'resolutionSource':'https://data.chain.link/streams/btc-usd-twap-60s-streams',
            'feesEnabled':True,
            'feeSchedule':{'rate':.07,'exponent':1,'takerOnly':True},'orderPriceMinTickSize':'.01','orderMinSize':5}
        return m,raw,gamma

    def test_current_public_protocol_fees_and_minimum_all_must_match(self):
        m,r,g=self.fixture();self.assertEqual(verify_rules(m,r,g)['maker_fee'],'0')
        for field,value in [('version','v2'),('orderMinSize',10),('clobTokenIds',['bad','md']),('acceptingOrders',False)]:
            wrong=deepcopy(g);wrong[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):verify_rules(m,r,wrong)
        r['fd'].pop('r')
        with self.assertRaises((ValueError,KeyError)):verify_rules(m,r,g)

    def test_resolution_requires_same_assets_final_zero_one_and_closed(self):
        m,r,g=self.fixture();group={'id':'m','market':'m','tokens':{'Up':'mu','Down':'md'}}
        g.update(closed=True,umaResolutionStatus='resolved',outcomePrices=['1','0']);self.assertEqual(verified_result(group,g),'Up')
        for k,v in [('closed',False),('outcomePrices',['.999','.001']),('conditionId','other'),('umaResolutionStatus','proposed')]:
            raw={**g,k:v};self.assertIsNone(verified_result(group,raw))

    def test_dynamic_clob_precision_uses_two_real_token_books_when_gamma_lags(self):
        m,r,g=self.fixture();r['mts']='.001'
        with self.assertRaisesRegex(ValueError,'精度变化'):verify_rules(m,r,g)
        real={side:{'asset_id':token,'market':'m','tick_size':'.001','min_order_size':'5'} for side,token in [('Up','mu'),('Down','md')]}
        accepted=verify_rules(m,r,g,real)
        self.assertEqual(D(accepted['tick_size']),D('.001'));self.assertIsNotNone(accepted['metadata_note'])
        real['Down']['asset_id']='different-market'
        with self.assertRaisesRegex(ValueError,'身份'):verify_rules(m,r,g,real)


if __name__=='__main__':unittest.main()
