"""按原附件 EX 编号执行的隔离合成账本测试；从不接触实盘账本。"""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import threading
import unittest

from polybot2other.hengzhun.ledger import HengzhunLedger, LedgerError, decimal, digest
from polybot2other.hengzhun.execution import PaperVenue, RealVenue, effective_delay
from polybot2other.hengzhun.replay import ReplayRunner, run_scenarios
from polybot2other.hengzhun.contracts import validate_record, contract_hash


SPACE={'mode':'PAPER','portfolio_run_id':'TEST','scenario_id':'S1','ledger_scope':'PAPER_PORTFOLIO'}


def cost(gross='10', price='.5', mode='NONE'):
    q,p=Decimal(gross),Decimal(price);cash=q*p
    fee_cash=Decimal('0.07')*q*p*(1-p) if mode=='CASH_ADDED' else Decimal('0')
    fee_shares=Decimal('.35') if mode=='SHARES_DEDUCTED' else Decimal('0')
    return {'fee_model_id':'synthetic-fee','fee_collection_mode':mode,'gross_shares':str(q),'net_shares':str(q-fee_shares),
        'cash_before_fees':str(cash),'fee_cash':str(fee_cash),'fee_shares':str(fee_shares),'all_in_cash':str(cash+fee_cash),
        'all_in_cost_per_net_share':str((cash+fee_cash)/(q-fee_shares)),
        'levels_used':[{'price':str(p),'gross_shares':str(q),'fee_cash':str(fee_cash),'fee_shares':str(fee_shares)}],
        'rounding_rule_id':'EXACT_SYNTHETIC'}


def plan(market='M1',strategy='HENGZHUN_TWAP_VALUE_V1',wallet='W1',scope='CALIBRATED'):
    return {'wallet_id':wallet,'strategy_id':strategy,'market':{'market_uid':market,'condition_id':market},
        'side':'UP','asset_id':market+'-UP','collateral_asset_id':'SYNTHETIC_USD','limit_price':'0.5','quantity_shares':'10','budget_all_in':'5',
        'contract_sha256':contract(market)['contract_sha256'],'config_sha256':'b'*64,'model_artifact_sha256':'c'*64 if scope=='CALIBRATED' else None,
        'decision_frame_id':'decision-'+market,'calibration_status':scope,'cost_quote':cost(),'window_close_at':300000}


def contract(market='M1',hold=0):
    evidence={'source_label':'SYNTHETIC_TEST','source_locator':None,'raw_payload_sha256':'d'*64,
              'observed_at':0,'verification_method':'synthetic-fixture','verified_at':0}
    value={'record_type':'MarketContractSnapshot','schema_version':'1.0.0','snapshot_id':market+'-contract','captured_at':0,
        'verification_status':'VERIFIED','verification_reasons':[],'evidence':[evidence],'contract_sha256':'a'*64,
        'market':{'market_uid':market,'condition_id':market},'slug':market,'market_protocol_version':'SYNTHETIC',
        'up_asset_id':market+'-UP','down_asset_id':market+'-DOWN','collateral_asset_id':'SYNTHETIC_USD','collateral_decimals':6,
        'listed_at':0,'window_open_at':0,'window_close_at':300000,'window_duration_seconds':300,
        'rules':{'rule_version':'synthetic-v1','rule_text_sha256':'e'*64,'resolution_feed_id':'synthetic-feed',
            'price_kind':'CHAINLINK_TWAP','symbol':'BTC/USD','twap_window_seconds':60,
            'comparison':'END_GREATER_THAN_OR_EQUAL_TO_START_UP','tie_policy':'UP','result_verification_rule_id':'synthetic-final'},
        'baseline':{'value':'100','source_at':0,'received_at':0,'verified_at':0,'status':'VERIFIED',
            'resolution_feed_id':'synthetic-feed','boundary_rule_id':'synthetic-boundary','verification_method':'synthetic','evidence':[evidence]},
        'versions':{'adapter_version':'synthetic-v1','adapter_code_sha256':'f'*64,'rest_schema_version':'synthetic-v1',
            'websocket_schema_version':'synthetic-v1','sdk_locked_version':'synthetic','signature_domain_version':None,'data_api_version':None},
        'trading':{'accepting_orders':True,'book_state':'READY','tick_size':'0.01','tick_size_unit':'COLLATERAL_PER_SHARE',
            'quantity_step':'1','quantity_unit':'SHARES','min_order_size':'5','min_order_size_unit':'SHARES','quantity_decimals':6,
            'supported_order_types':['FAK'],'venue_min_arrival_delay_ms':hold,'delay_status':'UNKNOWN' if hold is None else 'VERIFIED',
            'delay_included_in_scenario':True,'delay_evidence':[] if hold is None else [evidence],
            'fee_contract':{'verification_status':'VERIFIED','collection_mode':'NONE','fee_model_id':'synthetic-fee',
                'fee_formula_id':'ZERO_FEE','coefficient':'0','amount_semantics':'GROSS_SHARES','rounding_mode':'EXACT',
                'rounding_increment':'0.000001','minimum_fee':'0','evidence':[evidence]}}}
    value['contract_sha256']=contract_hash(value)
    return value


def book(at=240500,received=None,size='20',price='0.5',market='M1',seq=1,source=None):
    return {'record_type':'RawEvent','event_id':'book-'+str(seq),'boot_id':'test-boot','ingest_seq':seq,
        'source_kind':'ORDER_BOOK','market':{'market_uid':market,'condition_id':market},'asset_id':market+'-UP',
        'source_at':at if source is None else source,'received_at':at if received is None else received,
        'payload':{'complete':True,'book_hash':'book-'+str(seq),'contract_sha256':contract(market)['contract_sha256'],
                   'bids':[{'price':'0.49','size':'20'}],'asks':[{'price':price,'size':size}]}}


def result(market='M1',at=310000,outcome='UP',revision=1):
    return {'record_type':'OfficialResult','schema_version':'1.0.0','result_id':market+'-r'+str(revision),
        'market':{'market_uid':market,'condition_id':market},'contract_sha256':contract(market)['contract_sha256'],'revision':revision,'supersedes_result_id':None,
        'status':'VERIFIED_FINAL','source_at':300000,'received_at':at,'verified_at':at,'label_available_at':at,
        'official_final_verified':True,'outcome':outcome,'payouts':{'up':'1' if outcome=='UP' else '0','down':'0' if outcome=='UP' else '1'},
        'classification_label_up':1 if outcome=='UP' else 0,'baseline_value':None,'final_twap_value':None,
        'resolution_feed_id':'synthetic-feed','verification_rule_id':'synthetic-final',
        'evidence':[{'source_label':'synthetic','source_locator':None,'raw_payload_sha256':'d'*64,'observed_at':at,
                     'verification_method':'test-fixture','verified_at':at}],'reason_codes':[]}


def fee_adapter(c,side,p,q):return cost(q,p)


class HengzhunExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'ledger.sqlite3'
        self.ledger=HengzhunLedger(self.path,dict(SPACE));self.ledger.ensure_wallet('W1');self.addCleanup(lambda:self.ledger.close())
        self.venue=PaperVenue(self.ledger,fee_adapter)

    def reserve(self,market='M1',at=240000,**kw):return self.ledger.reserve(plan(market,**kw),at)['intent_id']

    def filled(self,market='M1',at=240000,quantity='10'):
        ident=self.reserve(market,at)
        self.ledger.submit_once(ident,at,at+1000);self.ledger.acknowledge(ident,at)
        self.ledger.record_match(ident,market+'-f','0.5',cost(quantity),at+1000)
        self.ledger.confirm_fill(market+'-f',at+13000);self.ledger.order_terminal(ident,at+13000,reconciled=True)
        return ident

    def test_ex001_ex002_live_disabled_and_record_has_no_economic_transactions(self):
        with self.assertRaisesRegex(LedgerError,'LIVE_EXECUTION_DISABLED'):RealVenue()
        with self.assertRaisesRegex(LedgerError,'LIVE_EXECUTION_DISABLED'):HengzhunLedger(Path(self.tmp.name)/'live.db',{**SPACE,'mode':'LIVE'})
        l=HengzhunLedger(Path(self.tmp.name)/'record.db',{**SPACE,'mode':'RECORD'});self.addCleanup(l.close);l.ensure_wallet('W1')
        with self.assertRaisesRegex(LedgerError,'RECORD_NO_ECONOMIC_ORDERS'):l.reserve(plan(),240000)
        self.assertEqual(l.intents(),[]);self.assertEqual(l.events(),[])

    def test_ex003_ex046_scopes_isolate_eligibility_and_do_not_merge_returns(self):
        self.reserve()
        shadow=HengzhunLedger(self.path,{**SPACE,'ledger_scope':'SIGNAL_SHADOW'});self.addCleanup(shadow.close);shadow.ensure_wallet('W1')
        shadow.reserve(plan(scope='UNCALIBRATED'),240000)
        self.assertEqual(len(shadow.intents()),1);self.assertEqual(len(self.ledger.intents()),1)
        self.assertFalse(shadow.snapshot('W1',240000)['account_return_valid'])

    def test_ex014_insufficient_budget_is_not_resized(self):
        l=HengzhunLedger(Path(self.tmp.name)/'small.db',dict(SPACE));self.addCleanup(l.close);l.ensure_wallet('W1',initial_cash='2')
        with self.assertRaisesRegex(LedgerError,'INSUFFICIENT_FULL_BUDGET'):l.reserve(plan(),240000)
        self.assertEqual(l.snapshot('W1',240000)['cash_reserved'],'0');self.assertEqual(l.intents(),[])

    def test_ex016_ex017_fees_are_exact_and_share_fees_not_double_charged(self):
        p=plan();p['cost_quote']=cost(mode='CASH_ADDED')
        with self.assertRaisesRegex(LedgerError,'BUDGET_EXCEEDED'):self.ledger.reserve(p,240000)
        ident=self.reserve();self.ledger.submit_once(ident,240000)
        self.ledger.record_match(ident,'sharefee','0.5',cost(mode='SHARES_DEDUCTED'),241000)
        self.ledger.confirm_fill('sharefee',253000);self.ledger.order_terminal(ident,253000,reconciled=True)
        self.assertEqual(self.ledger.snapshot('W1',253000)['cash_confirmed'],'95')
        self.assertEqual(self.ledger.fills(ident)[0]['net_shares'],'9.65')
        self.assertEqual(self.ledger.snapshot('W1',253000)['position_cost_unresolved'],'5')

    def test_ex018_atomic_cross_connection_concurrency_100_rounds(self):
        # Each round is its own immutable run; three SQLite connections compete.
        for index in range(100):
            ns={**SPACE,'portfolio_run_id':'concurrent-'+str(index)}
            ledgers=[HengzhunLedger(self.path,ns) for _ in range(3)]
            ledgers[0].ensure_wallet('W1');barrier=threading.Barrier(3)
            def try_reserve(i):
                barrier.wait()
                try:ledgers[i].reserve(plan(market='M'+str(i),strategy='S'+str(i)),240000);return True
                except LedgerError:return False
            with ThreadPoolExecutor(max_workers=3) as pool:self.assertEqual(sum(pool.map(try_reserve,range(3))),1)
            state=ledgers[0].snapshot('W1',240000)
            self.assertEqual((state['cash_reserved'],state['open_risk_slots_used'],state['claim_reserved_markets']),('5',1,1))
            for l in ledgers:l.close()

    def test_ex019_other_strategy_unknown_or_position_occupies_global_slot(self):
        ident=self.reserve(strategy='A');self.ledger.submit_once(ident,240000);self.ledger.mark_unknown(ident,240001)
        reasons=self.ledger.guard_reasons(plan('M2',strategy='B'),240010)
        self.assertIn('UNKNOWN_ORDER_OR_WALLET_FLIGHT',reasons);self.assertIn('WALLET_RISK_SLOT_FULL',reasons)
        self.ledger.record_match(ident,'small','0.5',cost('4'),241000);self.ledger.confirm_fill('small',253000)
        self.ledger.order_terminal(ident,253000,reconciled=True)
        reasons=self.ledger.guard_reasons(plan('M2',strategy='B'),253010)
        self.assertNotIn('UNKNOWN_ORDER_OR_WALLET_FLIGHT',reasons);self.assertIn('WALLET_RISK_SLOT_FULL',reasons)

    def test_ex020_ex048_zero_fill_aborted_and_rejected_never_restore_eligibility(self):
        for market,state in [('zero','TERMINAL_NO_FILL'),('abort','ABORTED_BEFORE_SEND'),('reject','REJECTED_CONFIRMED')]:
            ident=self.reserve(market)
            if state=='ABORTED_BEFORE_SEND':self.ledger.abort_before_send(ident,240100)
            else:
                self.ledger.submit_once(ident,240000)
                if state=='TERMINAL_NO_FILL':self.ledger.order_terminal(ident,241000,reconciled=True)
                else:self.ledger.reject_confirmed(ident,241000,evidence_verified=True)
            self.assertEqual(self.ledger.intent(ident)['state'],state)
            self.assertEqual(self.ledger.snapshot('W1',241000)['cash_reserved'],'0')
            for changed in ({'side':'DOWN'},{'config_sha256':'f'*64},{'model_artifact_sha256':'f'*64},{'strategy_id':'OTHER'}):
                p=plan(market);p.update(changed)
                with self.assertRaises(LedgerError):self.ledger.reserve(p,241000)
            with self.assertRaises(LedgerError):self.ledger.submit_once(ident,241001)

    def test_ex021_ex022_ex029_partial_fill_retains_reservation_until_whole_order_terminal(self):
        ident=self.reserve();self.ledger.submit_once(ident,240000)
        self.ledger.record_match(ident,'part','0.5',cost('4'),241000)
        self.ledger.confirm_fill('part',253000)
        for _ in range(3):self.ledger.confirm_fill('part',253000)
        state=self.ledger.snapshot('W1',253000)
        self.assertEqual((state['cash_confirmed'],state['cash_reserved'],state['position_cost_unresolved'],state['unresolved_max_loss']),('98','3','2','5'))
        self.assertEqual(state['open_risk_slots_used'],1)
        self.ledger.order_terminal(ident,253100,reconciled=True)
        state=self.ledger.snapshot('W1',253100)
        self.assertEqual((state['cash_reserved'],state['unresolved_max_loss']),('0','2'))
        self.assertEqual(len(self.ledger.fills(ident)),1)

    def test_ex023_ex024_ex026_unknown_survives_restarts_and_cannot_be_resent(self):
        ident=self.reserve();self.ledger.submit_once(ident,240000)
        self.ledger.recover(241000)
        self.assertEqual(self.ledger.intent(ident)['state'],'UNKNOWN')
        for at in (300000,999999999):
            self.ledger.recover(at)
            with self.assertRaises(LedgerError):self.ledger.submit_once(ident,at)
        self.assertEqual(self.ledger.snapshot('W1',999999999)['cash_reserved'],'5')
        self.ledger.record_match(ident,'late','0.5',cost(),241000)
        self.ledger.confirm_fill('late',999999999);self.ledger.order_terminal(ident,999999999,reconciled=True)
        self.assertEqual(self.ledger.snapshot('W1',999999999)['cash_confirmed'],'95')

    def test_ex025_restart_before_send_aborts_but_preserves_market_ownership(self):
        ident=self.reserve();self.ledger.recover(240100)
        self.assertEqual(self.ledger.intent(ident)['state'],'ABORTED_BEFORE_SEND')
        with self.assertRaises(LedgerError):self.reserve(strategy='another')

    def test_ex027_ex028_duplicate_and_out_of_order_callbacks_do_not_regress(self):
        ident=self.filled()
        self.ledger.acknowledge(ident,240000);self.ledger.mark_unknown(ident,239000)
        self.ledger.record_match(ident,'M1-f','0.5',cost(),241000)
        self.ledger.confirm_fill('M1-f',253000)
        self.assertEqual(self.ledger.intent(ident)['state'],'TERMINAL_WITH_FILL')
        self.assertEqual(self.ledger.snapshot('W1',253000)['cash_confirmed'],'95')
        self.assertEqual(sum(e['kind']=='CONFIRM_FILL' for e in self.ledger.events()),1)

    def test_ex009_arrival_never_uses_later_favorable_book(self):
        ident=self.reserve();self.venue.submit(ident,contract(),240000,clock_bound_ms=0)
        self.venue.on_event(book(at=240500,price='0.6'))
        self.venue.on_event(book(at=241100,price='0.4',seq=2))
        self.assertEqual(self.ledger.intent(ident)['state'],'TERMINAL_NO_FILL')
        self.assertEqual(self.ledger.fills(ident),[])

    def test_ex030_submit_and_expected_arrival_windows(self):
        for at in (209999,270001):
            ident=self.reserve(str(at),at)
            with self.assertRaisesRegex(LedgerError,'SUBMIT_TIME_WINDOW'):self.venue.submit(ident,contract(str(at)),at,clock_bound_ms=0)
        p=plan('late');p['contract_sha256']=contract('late',6000)['contract_sha256']
        ident=self.ledger.reserve(p,270000)['intent_id']
        with self.assertRaisesRegex(LedgerError,'EXPECTED_ARRIVAL_TOO_LATE'):self.venue.submit(ident,contract('late',6000),270000,clock_bound_ms=0)

    def test_ex031_delayed_arrival_breach_does_not_free_cancel_after_submit(self):
        ident=self.reserve();self.venue.submit(ident,contract(),240000,clock_bound_ms=0)
        fault={'event_id':'delay','boot_id':'test-boot','ingest_seq':1,'source_at':240100,'received_at':240100,'source_kind':'OTHER',
               'payload':{'fault_type':'DELAY_ARRIVAL','intent_id':ident,'arrival_at':280000}}
        self.venue.on_event(fault);self.venue.on_event(book(at=279500,seq=2))
        self.venue.advance(280000)
        self.assertEqual(len(self.ledger.fills(ident)),1)
        self.assertIn('ARRIVAL_GUARD_BREACH',self.ledger.snapshot('W1',280000)['halt_reasons'])

    def test_ex032_ex033_delay_hold_is_max_not_sum_and_unknown_not_zero(self):
        self.assertEqual(effective_delay('S1',contract(hold=250))['effective_total_delay_ms'],1000)
        self.assertEqual(effective_delay('S1',contract(hold=1500))['effective_total_delay_ms'],1500)
        value=effective_delay('S1',contract(hold=None))
        self.assertIsNone(value['venue_hold_ms']);self.assertEqual(value['venue_hold_status'],'UNKNOWN')
        self.assertEqual(value['execution_evidence'],'ASSUMED')
        with self.assertRaises(LedgerError):effective_delay('EMPIRICAL',contract())

    def test_ex034_invalid_timing_profile_halts_without_liquidation(self):
        ident=self.reserve()
        with self.assertRaisesRegex(LedgerError,'TIME_BOUNDS_UNKNOWN'):self.venue.submit(ident,contract(),240000,clock_bound_ms=None)
        self.assertIn('TIME_BOUNDS_UNKNOWN',self.ledger.snapshot('W1',240000)['halt_reasons'])
        self.assertEqual(self.ledger.fills(ident),[])

    def test_ex035_confirm_delay_starts_at_match_not_submission(self):
        ident=self.reserve();self.venue.submit(ident,contract(),240000,clock_bound_ms=0)
        self.venue.on_event(book());self.venue.advance(241000)
        self.assertEqual(self.ledger.fills(ident)[0]['state'],'MATCHED')
        self.assertEqual(self.ledger.snapshot('W1',241000)['unresolved_max_loss'],'5')
        self.venue.advance(252000);self.assertEqual(self.ledger.snapshot('W1',252000)['cash_confirmed'],'100')
        self.venue.advance(253000);self.assertEqual(self.ledger.snapshot('W1',253000)['cash_confirmed'],'95')
        self.assertEqual(self.ledger.fills(ident)[0]['payload']['confirmed_at'],253000)

    def test_ex036_ex038_four_settlement_conditions_gate_claim_clock(self):
        ident=self.reserve();self.ledger.submit_once(ident,240000)
        self.ledger.mark_unknown(ident,240001);self.ledger.record_match(ident,'unclear','0.5',cost('8'),241000)
        self.ledger.record_result(result(at=300001),300001)
        self.assertIsNone(self.ledger.intent(ident)['recognized_at'])
        self.ledger.confirm_fill('unclear',300500)
        self.assertIsNone(self.ledger.intent(ident)['recognized_at'])
        self.ledger.order_terminal(ident,302500,reconciled=True)
        order=self.ledger.intent(ident)
        self.assertEqual(order['recognized_at'],302500);self.assertEqual(order['claim_available_at'],362500)
        state=self.ledger.snapshot('W1',302500)
        self.assertEqual((state['cash_confirmed'],state['claim_receivable'],state['claim_cost_committed'],state['unresolved_max_loss']),('96','8','4','0'))

    def test_ex037_loss_has_no_second_cash_debit(self):
        ident=self.filled();self.ledger.record_result(result(outcome='DOWN'),310000)
        state=self.ledger.snapshot('W1',310000)
        self.assertEqual((state['cash_confirmed'],state['realized_pnl'],state['rolling_24h_gross_loss']),('95','-5','5'))
        self.assertEqual(state['committed_cost_total'],'0');self.assertEqual(state['open_risk_slots_used'],0)

    def test_ex039_two_claim_slots_block_even_when_costs_small(self):
        for index in range(2):
            ident=self.filled('M'+str(index),quantity='2')
            self.ledger.record_result(result('M'+str(index)),310000)
        state=self.ledger.snapshot('W1',310001)
        self.assertEqual(state['claim_pending_markets'],2);self.assertEqual(state['claim_cost_committed'],'2')
        self.assertIn('CLAIM_SLOT_LIMIT',self.ledger.guard_reasons(plan('M3'),310001))
        self.assertNotIn('COMMITTED_COST_LIMIT',self.ledger.guard_reasons(plan('M3'),310001))

    def test_ex040_claim_is_idempotent_asset_conversion(self):
        ident=self.filled(quantity='8');self.ledger.record_result(result(),310000)
        with self.assertRaises(LedgerError):self.ledger.claim(ident,'receipt','8',369999)
        self.ledger.claim(ident,'receipt','8',370000);self.ledger.claim(ident,'receipt','8',370000)
        state=self.ledger.snapshot('W1',370000)
        self.assertEqual((state['cash_confirmed'],state['realized_pnl'],state['claim_receivable']),('104','4','0'))
        self.assertEqual(state['committed_cost_total'],'0');self.assertEqual(state['claim_pending_markets'],0)

    def test_ex041_gross_losses_do_not_net_winners(self):
        for market,qty,outcome in [('loss1','10','DOWN'),('win','8','UP'),('loss2','10','DOWN')]:
            ident=self.filled(market,quantity=qty);self.ledger.record_result(result(market,outcome=outcome),310000)
            if outcome=='UP':self.ledger.claim(ident,'claim-win','8',370000)
        state=self.ledger.snapshot('W1',400000)
        self.assertEqual(state['rolling_24h_gross_loss'],'10');self.assertEqual(state['realized_pnl'],'-6')
        self.assertIn('GROSS_LOSS_RISK_LIMIT',self.ledger.guard_reasons(plan('next'),400000))

    def test_ex042_ex043_drawdown_latches_across_restart_and_date(self):
        for index,at in enumerate((240000,600000,90000000)):
            market='loss-'+str(index);p=plan(market);p['window_close_at']=at+60000
            ident=self.ledger.reserve(p,at)['intent_id'];self.ledger.submit_once(ident,at)
            self.ledger.record_match(ident,market+'-fill','0.5',cost(),at+1000)
            self.ledger.confirm_fill(market+'-fill',at+13000);self.ledger.order_terminal(ident,at+13000,reconciled=True)
            r=result(market,at=at+60001,outcome='DOWN');r['source_at']=at+60000
            self.ledger.record_result(r,at+60001)
        state=self.ledger.snapshot('W1',90060001)
        self.assertEqual(state['cash_confirmed'],'85');self.assertEqual(state['high_water_nav'],'100')
        self.assertTrue(state['drawdown_halt'])
        self.ledger.close();self.ledger=HengzhunLedger(self.path,dict(SPACE));self.ledger.recover(200000000)
        state=self.ledger.snapshot('W1',200000000)
        self.assertEqual(state['rolling_24h_gross_loss'],'0');self.assertTrue(state['drawdown_halt'])
        self.assertIn('DRAWDOWN_HALT',self.ledger.guard_reasons(plan('next'),200000000))

    def test_ex044_missing_valuation_is_unknown_not_zero_or_fake_drawdown(self):
        self.filled();self.ledger.mark_equity('W1',None,260000,complete=False)
        state=self.ledger.snapshot('W1',260000)
        self.assertIsNone(state['nav']);self.assertFalse(state['drawdown_halt']);self.assertEqual(state['unresolved_max_loss'],'5')
        self.assertIn('VALUATION_UNCERTAIN',self.ledger.guard_reasons(plan('M2'),260000))

    def test_ex045_cash_mismatch_preserves_expected_ledger_and_halts(self):
        self.assertFalse(self.ledger.reconcile_cash('W1','96',1000))
        state=self.ledger.snapshot('W1',1000)
        self.assertEqual(state['cash_confirmed'],'100');self.assertIn('WALLET_RECONCILIATION_MISMATCH',state['halt_reasons'])

    def test_ex047_unknown_and_unsettled_do_not_disappear_from_report(self):
        ident=self.reserve();self.ledger.submit_once(ident,240000);self.ledger.mark_unknown(ident,240001)
        state=self.ledger.snapshot('W1',310000)
        self.assertEqual(state['states'],{'UNKNOWN':1});self.assertTrue(state['unknown_exposure']);self.assertEqual(state['unresolved_max_loss'],'5')

    def test_ex049_retrying_fill_cannot_be_called_zero_fill(self):
        ident=self.reserve();self.ledger.submit_once(ident,240000);self.ledger.record_match(ident,'r','0.5',cost(),241000)
        self.ledger.fill_status('r','RETRYING',242000)
        self.ledger.order_terminal(ident,243000,reconciled=True)
        self.assertNotEqual(self.ledger.intent(ident)['state'],'TERMINAL_NO_FILL')
        self.assertEqual(self.ledger.snapshot('W1',243000)['cash_reserved'],'5')
        with self.assertRaises(LedgerError):self.ledger.fill_status('r','FAILED_FINAL',244000)
        self.ledger.fill_status('r','FAILED_FINAL',244000,permanent_no_payment_proven=True)
        self.assertEqual(self.ledger.intent(ident)['state'],'TERMINAL_NO_FILL')

    def test_ex050_three_scenarios_replay_completely_and_reproduce(self):
        events=[book(at=240500,seq=1),book(at=241500,seq=2),book(at=244500,seq=3)]
        def factory(scenario):
            def decide(event,venue):
                if not venue.ledger.intents():
                    venue.ledger.reserve(plan(wallet='HENGZHUN'),event['received_at'])
                    ident=venue.ledger.intents()[0]['intent_id'];venue.submit(ident,contract(),event['received_at'],clock_bound_ms=0)
            return decide
        a=run_scenarios(events,Path(self.tmp.name)/'a',fee_adapter,{},factory,final_at=270000)
        b=run_scenarios(events,Path(self.tmp.name)/'b',fee_adapter,{},factory,final_at=270000)
        self.assertEqual(a,b);self.assertEqual(set(a['scenarios']),{'S1','S2','S3'})
        self.assertTrue(all(r['execution_evidence']=='ASSUMED' for r in a['scenarios'].values()))

    def test_contract_records_validate_against_supplied_schema(self):
        ident=self.filled(quantity='8');self.ledger.record_result(result(),310000);self.ledger.claim(ident,'r','8',370000)
        validate_record(self.ledger.intent(ident)['payload'],definition='OrderIntent')
        for fill in self.ledger.fills(ident):validate_record(fill['payload'],definition='Fill')
        for event in self.ledger.events():validate_record(event,definition='LedgerEvent')
        validate_record(result(),definition='OfficialResult')

    def test_revision_pauses_without_overwriting_prior_result_or_profit(self):
        ident=self.filled();self.ledger.record_result(result(),310000)
        before=self.ledger.snapshot('W1',310000)
        self.ledger.record_result(result(outcome='DOWN',at=320000,revision=2),320000)
        after=self.ledger.snapshot('W1',320000)
        self.assertEqual(after['realized_pnl'],before['realized_pnl'])
        self.assertIn('SETTLEMENT_EXCEPTION',after['halt_reasons'])
        self.assertIsNone(after['nav'])
        self.ledger.mark_equity('W1','0',320001)
        self.assertIsNone(self.ledger.snapshot('W1',320001)['nav'])
        self.assertEqual(self.ledger.conn.execute('SELECT count(*) FROM hz_results').fetchone()[0],2)

    def test_verified_unclaimed_revision_reverses_original_and_preserves_other_halts(self):
        ident=self.filled();self.ledger.record_result(result(),310000)
        original=[e for e in self.ledger.events() if e['kind']=='SETTLEMENT_RECOGNIZED_CLAIM'][0]
        revised=result(outcome='DOWN',at=320000,revision=2);revised['supersedes_result_id']='M1-r1'
        self.ledger.record_result(revised,320000)
        self.ledger.halt('W1','WALLET_RECONCILIATION_MISMATCH',320000)
        self.assertTrue(self.ledger.reconcile_result_revision('M1','M1-r2',321000,evidence_hashes=['a'*64]))
        state=self.ledger.snapshot('W1',321000)
        self.assertEqual((state['cash_confirmed'],state['realized_pnl'],state['claim_receivable'],state['rolling_24h_gross_loss']),('95','-5','0','5'))
        self.assertEqual(state['committed_cost_total'],'0')
        self.assertNotIn('SETTLEMENT_EXCEPTION',state['halt_reasons'])
        self.assertIn('WALLET_RECONCILIATION_MISMATCH',state['halt_reasons'])
        reversals=[e for e in self.ledger.events() if e['kind']=='REVERSAL']
        self.assertEqual(len(reversals),1);self.assertEqual(reversals[0]['reverses_ledger_event_id'],original['ledger_event_id'])
        self.assertEqual({d['account']:Decimal(d['delta']) for d in reversals[0]['deltas']},
                         {d['account']:-Decimal(d['delta']) for d in original['deltas']})
        self.assertFalse(self.ledger.reconcile_result_revision('M1','M1-r2',322000,evidence_hashes=['a'*64]))
        self.assertEqual(self.ledger.intent(ident)['result_id'],'M1-r2')
        for event in self.ledger.events():validate_record(event,definition='LedgerEvent')

    def test_revision_from_loss_to_win_creates_new_claim_and_new_claim_clock(self):
        ident=self.filled();self.ledger.record_result(result(outcome='DOWN'),310000)
        revised=result(at=320000,revision=2);revised['supersedes_result_id']='M1-r1'
        self.ledger.record_result(revised,320000)
        self.ledger.reconcile_result_revision('M1','M1-r2',321000,evidence_hashes=['a'*64])
        self.assertEqual(self.ledger.intent(ident)['claim_available_at'],381000)
        self.assertEqual(self.ledger.snapshot('W1',321000)['rolling_24h_gross_loss'],'0')
        self.ledger.claim(ident,'revised-claim','10',381000)
        state=self.ledger.snapshot('W1',381000)
        self.assertEqual((state['cash_confirmed'],state['realized_pnl'],state['claim_receivable']),('105','5','0'))
        event=[e for e in self.ledger.events() if e['kind']=='CLAIM_RECEIVED'][-1]
        self.assertEqual(event['settlement_gate']['official_result_id'],'M1-r2')
        for event in self.ledger.events():validate_record(event,definition='LedgerEvent')

    def test_claimed_revision_needs_verified_matching_cash_receipt_and_is_atomic(self):
        ident=self.filled();self.ledger.record_result(result(),310000)
        self.ledger.claim(ident,'original-claim','10',370000)
        revised=result(outcome='DOWN',at=380000,revision=2);revised['supersedes_result_id']='M1-r1'
        self.ledger.record_result(revised,380000)
        before=self.ledger.snapshot('W1',380000);event_count=len(self.ledger.events())
        with self.assertRaisesRegex(LedgerError,'REVISION_CASH_DELTA_NOT_VERIFIED'):
            self.ledger.reconcile_result_revision('M1','M1-r2',381000,evidence_hashes=['a'*64])
        self.assertEqual(self.ledger.snapshot('W1',380000),before);self.assertEqual(len(self.ledger.events()),event_count)
        receipt={'receipt_id':'verified-return','cash_delta':'-10','received_at':380100,'verified_at':380200,
                 'verified':True,'evidence_hashes':['b'*64]}
        self.ledger.reconcile_result_revision('M1','M1-r2',381000,evidence_hashes=['a'*64],cash_adjustments={ident:receipt})
        state=self.ledger.snapshot('W1',381000)
        self.assertEqual((state['cash_confirmed'],state['realized_pnl'],state['claim_receivable']),('95','-5','0'))
        self.assertEqual(self.ledger.intent(ident)['position_state'],'CLOSED_CLAIMED')
        # All journal deltas still reconstruct the confirmed balance and PnL.
        self.assertEqual(sum(Decimal(d['delta']) for e in self.ledger.events() for d in e['deltas'] if d['account']=='CASH_CONFIRMED'),Decimal('95'))
        self.assertEqual(sum(Decimal(d['delta']) for e in self.ledger.events() for d in e['deltas'] if d['account']=='REALIZED_PNL'),Decimal('-5'))
        for event in self.ledger.events():validate_record(event,definition='LedgerEvent')

    def test_unlinked_revision_cannot_rewrite_settlement(self):
        self.filled();self.ledger.record_result(result(),310000)
        self.ledger.record_result(result(outcome='DOWN',at=320000,revision=2),320000)
        with self.assertRaisesRegex(LedgerError,'REVISION_CHAIN_UNVERIFIED'):
            self.ledger.reconcile_result_revision('M1','M1-r2',321000,evidence_hashes=['a'*64])
        self.assertIn('SETTLEMENT_EXCEPTION',self.ledger.snapshot('W1',321000)['halt_reasons'])

    def test_pending_result_allows_unknown_source_time_but_future_label_is_rejected(self):
        pending=result();pending.update(source_at=None,verified_at=None,label_available_at=None,official_final_verified=False,
            status='PENDING',outcome='UNKNOWN',payouts=None,classification_label_up=None)
        self.assertFalse(self.ledger.record_result(pending,310000))
        future=result('M2');future['label_available_at']=310001
        with self.assertRaisesRegex(LedgerError,'FUTURE_RESULT'):self.ledger.record_result(future,310000)

    def test_latest_invalid_book_tombstones_an_earlier_valid_quote(self):
        ident=self.reserve();self.venue.submit(ident,contract(),240000,clock_bound_ms=0)
        self.venue.on_event(book())
        invalid=book(at=240900,seq=2);invalid['payload']['complete']=False
        self.venue.on_event(invalid);self.venue.advance(241000)
        self.assertEqual(self.ledger.intent(ident)['state'],'TERMINAL_NO_FILL')
        self.assertEqual(self.ledger.fills(ident),[])

    def test_venue_checks_collateral_minimum_and_ignores_zero_quantity_levels(self):
        c=contract();c['trading']['min_order_size_unit']='PRE_FEE_COLLATERAL';c['trading']['min_order_size']='6'
        c['contract_sha256']=contract_hash(c);p=plan();p['contract_sha256']=c['contract_sha256']
        ident=self.ledger.reserve(p,240000)['intent_id']
        with self.assertRaisesRegex(LedgerError,'BELOW_MARKET_MINIMUM'):
            self.venue.submit(ident,c,240000,clock_bound_ms=0)
        ident=self.reserve('M2');self.venue.submit(ident,contract('M2'),240000,clock_bound_ms=0)
        event=book(market='M2');event['payload']['bids'].append({'price':'0.9','size':'0'})
        self.venue.on_event(event);self.venue.advance(253000)
        self.assertEqual(self.ledger.intent(ident)['state'],'TERMINAL_WITH_FILL')

    def test_revision_before_terminal_reconciliation_does_not_bypass_settlement_gate(self):
        ident=self.reserve();self.ledger.submit_once(ident,240000)
        self.ledger.record_match(ident,'pending','0.5',cost(),241000)
        self.ledger.record_result(result(),310000)
        revised=result(outcome='DOWN',at=320000,revision=2);revised['supersedes_result_id']='M1-r1'
        self.ledger.record_result(revised,320000)
        self.ledger.reconcile_result_revision('M1','M1-r2',321000,evidence_hashes=['a'*64])
        self.assertIsNone(self.ledger.intent(ident)['recognized_at'])
        self.ledger.confirm_fill('pending',322000);self.ledger.order_terminal(ident,322000,reconciled=True)
        self.assertEqual(self.ledger.intent(ident)['result_id'],'M1-r2')
        self.assertEqual(self.ledger.snapshot('W1',322000)['realized_pnl'],'-5')


    def test_disconnection_never_backdates_confirmation_receipt(self):
        ident=self.reserve();self.venue.submit(ident,contract(),240000,clock_bound_ms=0)
        self.venue.on_event(book())
        self.venue.on_event({'event_id':'disconnect','boot_id':'test-boot','ingest_seq':2,'source_at':241500,'received_at':241500,
                             'source_kind':'OTHER','payload':{'fault_type':'DISCONNECT'}})
        self.venue.on_event({'event_id':'reconnect','boot_id':'test-boot','ingest_seq':3,'source_at':270000,'received_at':270000,
                             'source_kind':'OTHER','payload':{'fault_type':'RECONNECT'}})
        self.assertEqual(self.ledger.fills(ident)[0]['payload']['confirmed_at'],270000)

    def test_canonical_book_view_fields_are_supported(self):
        ident=self.reserve();self.venue.submit(ident,contract(),240000,clock_bound_ms=0)
        event=book();view=event['payload'];view['is_complete']=view.pop('complete');view['snapshot_sha256']=view.pop('book_hash')
        for level in view['asks']+view['bids']:level['quantity_shares']=level.pop('size')
        self.venue.on_event(event);self.venue.advance(253000)
        self.assertEqual(self.ledger.intent(ident)['state'],'TERMINAL_WITH_FILL')


    def test_repeat_submit_cannot_rewrite_persisted_venue_arrival(self):
        ident=self.reserve();self.venue.submit(ident,contract(),240000,clock_bound_ms=0)
        before=self.venue._task(ident)
        with self.assertRaisesRegex(LedgerError,'INTENT_ALREADY_SENT_OR_TERMINAL'):
            self.venue.submit(ident,contract(),250000,clock_bound_ms=0)
        self.assertEqual(self.venue._task(ident),before)

    def test_venue_rejects_a_forged_verified_contract_hash(self):
        ident=self.reserve();c=contract();c['trading']['quantity_step']='0.1'
        with self.assertRaisesRegex(LedgerError,'CONTRACT_UNVERIFIED'):
            self.venue.submit(ident,c,240000,clock_bound_ms=0)
        self.assertEqual(self.ledger.intent(ident)['state'],'ABORTED_BEFORE_SEND')
        self.assertEqual(self.ledger.snapshot('W1',240000)['cash_reserved'],'0')
