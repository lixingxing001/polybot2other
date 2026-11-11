"""V2 tests use isolated synthetic public events, never a live order client."""
from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from polybot2other.paired_v2 import PairV2Engine
from polybot2other.paired_paper import canonical,fee
from polybot2other.paired_v2_service import exit_contract,ExitContractError
from test_paired_paper import market,books,print_,rules


def terms(at):return {**rules(),'checked_at':at}


class PairV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'main.sqlite3';self.e=PairV2Engine(self.path,at_ms=0)
        self.addCleanup(lambda:self.e.close())

    def tick(self,at,**kw):self.e.tick(at,market(),books(at,**kw),terms(at))

    def opened(self):
        for at in (30000,30500,31000,31500,32000):self.tick(at)
        self.assertEqual(len(self.e.orders()),2)
        self.tick(33900);self.tick(34000)
        self.assertEqual([o['status'] for o in self.e.orders(active=True)],['OPEN','OPEN'])

    def seed_pnl(self,values):
        with self.e.tx():
            total=sum((D(p) for _,p in values),D(0))
            self.e.conn.execute('UPDATE pair_wallet SET cash=?,realized=?',(str(100+total),str(total)))
            for at,p in values:self.e.conn.execute("INSERT INTO pair_profit(at,group_id,pnl,kind) VALUES(?,'synthetic',?,'TEST')",(at,p))

    def test_no_training_gate_but_causal_short_stability_required(self):
        self.tick(30000);self.assertEqual(self.e.orders(),[])
        for at in (30500,31000,31500,32000):self.tick(at)
        self.assertEqual(len(self.e.orders()),2)
        self.assertEqual({o['price'] for o in self.e.orders()},{'0.48'})
        self.assertLessEqual(self.e.reserved(),5)
        snap=self.e.snapshot(32000);self.assertEqual(snap['strategy_id'],'PAIRED_MAKER_V2');self.assertFalse(snap['live_enabled'])

    def test_fast_jump_and_extreme_price_dont_quote(self):
        for at in (30000,30500,31000,31500):self.tick(at)
        self.tick(32000,up='.55',down='.43');self.assertEqual(self.e.orders(),[])
        for at in (33000,34000,35000,36000,37000):self.tick(at,up='.80',down='.18')
        self.assertEqual(self.e.orders(),[])

    def test_v1_loss_incident_rejects_next_five_dollar_group_before_submission(self):
        self.seed_pnl([(10000,'-9.171470')])
        self.tick(32000)
        self.assertEqual(self.e.last_code,'RISK_BUDGET');self.assertEqual(self.e.orders(),[])
        r=self.e.risk_snapshot(32000);self.assertEqual(D(r['available']),D('.828530'));self.assertFalse(r['new_group_allowed'])

    def test_winners_cannot_restore_gross_loss_allowance(self):
        self.seed_pnl([(10000,'-7'),(20000,'20')])
        r=self.e.risk_snapshot(30000)
        self.assertEqual(D(self.e.wallet()['realized']),13)
        self.assertEqual(D(r['gross_loss_24h']),7);self.assertFalse(r['new_group_allowed'])

    def test_pending_orders_and_unconfirmed_fills_reserve_risk(self):
        self.opened();before=self.e.risk_snapshot(34000)
        self.assertEqual(D(before['order_reservations']),D('4.9920'))
        self.e.tape(print_(35000,price='.48',size='1000'),35000)
        after=self.e.risk_snapshot(35000)
        self.assertEqual(D(after['unresolved_risk']),D('4.9920'))
        self.assertGreater(D(after['position_max_loss']),0)
        self.assertGreater(D(after['order_reservations']),0)

    def test_confirmed_pair_releases_risk_but_not_claim_cash(self):
        self.opened();self.e.tape(print_(35000,price='.48',size='1000'),35000)
        self.e.tape(print_(35000,'Down',price='.48',size='1000'),35000)
        self.assertGreater(D(self.e.risk_snapshot(35000)['unresolved_risk']),4)
        self.tick(47000)
        self.assertEqual(D(self.e.risk_snapshot(47000)['unresolved_risk']),0)
        s=self.e.snapshot(47000);self.assertEqual(D(s['claim_receivable']),D('5.20'));self.assertEqual(D(s['cash']),D('95.0080'))
        self.tick(107000);self.assertEqual(D(self.e.wallet()['cash']),D('100.2080'))

    def test_order_reservation_checks_transaction_budget(self):
        self.opened();g=self.e.groups()[0]
        before=len(self.e.orders())
        with self.assertRaisesRegex(ValueError,'RISK_BUDGET'):
            self.e.new_order(g,'Up','.5','11',35000)
        self.assertEqual(len(self.e.orders()),before)
        self.assertLessEqual(D(self.e.risk_snapshot(35000,next_budget=0)['used']),10)

    def test_lifetime_loss_room_and_restart_cannot_reset_allowance(self):
        self.seed_pnl([(10000,'-6')]);now=90000000
        self.assertEqual(D(self.e.risk_snapshot(now)['gross_loss_24h']),0)
        self.assertFalse(self.e.risk_snapshot(now)['new_group_allowed'])
        self.e.close();self.e=PairV2Engine(self.path,at_ms=now)
        self.assertFalse(self.e.risk_snapshot(now)['new_group_allowed']);self.assertEqual(D(self.e.wallet()['cash']),94)
        self.e.settings({'enabled':False},now);self.e.settings({'enabled':True},now+1)
        self.assertFalse(self.e.risk_snapshot(now+1)['new_group_allowed'])

    def test_source_clock_and_last_receive_do_not_refresh_old_fee_evidence(self):
        self.opened();g=self.e.groups()[0]
        self.e.set_exit_rules('m',terms(34000),books(34000),34000)
        self.e.add_book(books(60000)['Up'],60000)
        self.assertIsNone(self.e._usable_exit_rules(g,60000))

    def test_independent_exit_terms_work_when_entry_rules_missing(self):
        self.opened();self.e.tape(print_(35000,price='.48',size='1000'),35000)
        self.tick(37000);self.tick(39000);self.tick(45000)
        self.e.set_exit_rules('m',terms(47000),books(47000,up='.45',down='.53'),47000)
        self.e.tick(47000,market(),books(47000,up='.45',down='.53'),None)
        exits=[o for o in self.e.orders() if o['kind']=='EXIT']
        self.assertEqual(len(exits),1);self.assertNotIn('m',self.e.rules)
        self.assertEqual(self.e.groups()[0]['exit_code'],'EXITING')

    def test_confirmed_subset_can_exit_while_last_fill_still_unconfirmed(self):
        self.opened();self.e.tape(print_(35000,price='.48',size='20'),35000) # 5 shares
        self.tick(37000)
        self.e.tape(print_(38000,price='.48',size='.8'),40000) # another .2 shares pending until 52000
        self.tick(39000);self.tick(45000)
        self.e.set_exit_rules('m',terms(47000),books(47000,up='.45',down='.53'),47000)
        self.e.tick(47000,market(),books(47000,up='.45',down='.53'),None)
        exits=[o for o in self.e.orders() if o['kind']=='EXIT']
        self.assertEqual(len(exits),1);self.assertEqual(D(exits[0]['quantity']),5)
        self.assertTrue(self.e._pending('m'))

    def test_confirmed_dust_is_labelled_small_tail_not_permanent_confirmation_wait(self):
        self.opened();self.e.tape(print_(35000,price='.48',size='.01'),35000)
        self.tick(37000);self.tick(39000);self.tick(45000);self.tick(47000)
        g=self.e.groups()[0]
        self.assertFalse(self.e._pending('m'));self.assertEqual(g['exit_code'],'BELOW_EXIT_MINIMUM');self.assertEqual(g['phase'],'SMALL_TAIL')

    def test_exit_supports_bid_only_book_and_never_uses_later_or_empty_bid(self):
        self.opened();b=books(35000)['Up'];b.update(source='paired-v2-exit-rest',asks=[],best_ask=None)
        self.e.add_book(b,35000);self.assertIsNotNone(self.e.exit_book_at('mu',35000))
        self.assertIsNone(self.e.exit_book_at('mu',34999,after=34000))
        empty=deepcopy(b);empty.update(source_ms=35100,received_ms=35100,bids=[],best_bid=None)
        self.e.add_book(empty,35100);self.assertIsNone(self.e.exit_book_at('mu',35100))

    def test_signal_and_stress_ledgers_remain_separate_and_both_visible(self):
        stress=PairV2Engine(Path(self.tmp.name)/'stress.sqlite3',at_ms=0,scenario='STRESS');self.addCleanup(stress.close)
        for e in (self.e,stress):
            for at in (30000,30500,31000,31500,32000,33900,34000):e.tick(at,market(),books(at),terms(at))
            e.tick(35000,market(),books(35000,up='.45'),terms(35000))
        self.assertEqual(self.e.snapshot(35000)['fills'],[])
        self.assertTrue(stress.snapshot(35000)['fills'])
        self.assertEqual(D(self.e.wallet()['cash']),100)
        self.assertLess(D(stress.wallet()['cash']),100)

    def test_pricing_moves_trigger_cancel_before_quote_ttl(self):
        self.opened();self.tick(35000,up='.52',down='.46')
        self.assertTrue(any(o['status']=='CANCELING' for o in self.e.orders()))

    def test_exit_failure_escalates_limit_and_continues_without_three_attempt_cap(self):
        self.opened();self.e.tape(print_(35000,price='.48',size='1000'),35000)
        self.tick(37000);self.tick(39000);self.tick(45000)
        g=self.e.groups()[0];g['rescue_attempts']=4;g['retry_after']=0
        with self.e.tx():self.e.save_group(g)
        self.e.set_exit_rules('m',terms(47000),books(47000,up='.30',down='.68'),47000)
        self.e.tick(47000,market(),books(47000,up='.30',down='.68'),None)
        exit_=next(o for o in self.e.orders() if o['kind']=='EXIT')
        self.assertEqual(D(exit_['price']),D('.01'))
        self.e.tick(48900,market(),books(48900,up='.28',down='.70'),None)
        self.e.tick(49000,market(),books(49000,up='.28',down='.70'),None)
        sell=[f for f in self.e.snapshot(49000)['fills'] if f['action']=='SELL'][0]
        self.assertEqual(D(sell['price']),D('.28')) # actual depth, not the low limit

    def test_quote_cache_does_not_grow_with_all_historical_assets(self):
        self.e.add_book(books(30000,key='old')['Up'],30000)
        self.tick(30000)
        self.assertNotIn('oldu',self.e.history)
        self.assertIn('mu',self.e.history)


class ExitContractTests(unittest.TestCase):
    def fixture(self):
        g={'id':'m','tokens':{'Up':'mu','Down':'md'}}
        raw={'c':'m','v':'v1','ao':True,'t':[{'o':'Up','t':'mu'},{'o':'Down','t':'md'}],
             'fd':{'r':'.07','e':1,'to':True},'mts':'.01','mos':5}
        b={'market':'m','asset_id':'mu','timestamp':'1000','tick_size':'.01','min_order_size':5,
           'bids':[{'price':'.4','size':'20'}],'asks':[]}
        return g,raw,b

    def test_exit_proof_needs_no_gamma_but_checks_current_clob_identity_fee_and_clock(self):
        g,r,b=self.fixture();rule,view=exit_contract(g,r,{'Up':b},1100)
        self.assertEqual(rule['verification_scope'],'CURRENT_CLOB_EXIT_ONLY');self.assertEqual(view['Up']['asks'],[])
        with self.assertRaisesRegex(ValueError,'源时间'):exit_contract(g,r,{'Up':b},5000)
        r['v']='v2'
        with self.assertRaises(ExitContractError):exit_contract(g,r,{'Up':b},1100)

    def test_valid_metadata_can_refresh_fee_proof_without_fake_book(self):
        g,r,b=self.fixture();rule,view=exit_contract(g,r,{},1000,metadata_only=True)
        self.assertEqual(view,{});self.assertEqual(rule['checked_at'],1000)
        r['fd']={}
        with self.assertRaises(ExitContractError):exit_contract(g,r,{},1000,metadata_only=True)

    def test_slow_second_book_never_relabels_first_book_receipt(self):
        g,r,b=self.fixture();down={**b,'asset_id':'md','timestamp':'1150'}
        _,views=exit_contract(g,r,{'Up':b,'Down':down},1300,received_by_side={'Up':1100,'Down':1300})
        self.assertEqual(views['Up']['received_ms'],1100);self.assertEqual(views['Down']['received_ms'],1300)
        with self.assertRaises(ValueError):exit_contract(g,r,{'Down':down},1300,received_by_side={'Down':1100})


if __name__=='__main__':unittest.main()
