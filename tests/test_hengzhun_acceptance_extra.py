"""Independent acceptance gaps, using synthetic economics and no network clients."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

import test_hengzhun_execution as fixture
from polybot2other.hengzhun.contracts import contract_hash,validate_record
from polybot2other.hengzhun.execution import PaperVenue, RealVenue
from polybot2other.hengzhun.ledger import HengzhunLedger, LedgerError, digest
from polybot2other.hengzhun.runtime import HengzhunRuntime


class AcceptanceExtraTests(unittest.TestCase):
    reserve=fixture.HengzhunExecutionTests.reserve
    filled=fixture.HengzhunExecutionTests.filled

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'ledger.sqlite3'
        self.ledger=HengzhunLedger(self.path,dict(fixture.SPACE));self.ledger.ensure_wallet('W1')
        self.addCleanup(lambda:self.ledger.close());self.venue=PaperVenue(self.ledger,fixture.fee_adapter)

    def test_ex001_live_startup_never_touches_supplied_signer_or_mutation_transport(self):
        signer=Mock(side_effect=AssertionError('signer must not be touched'))
        transport=Mock(side_effect=AssertionError('mutation must not be touched'))
        feed=Mock(side_effect=AssertionError('feed must not be touched'))
        with self.assertRaisesRegex(ValueError,'LIVE_EXECUTION_DISABLED'):
            HengzhunRuntime(Path(self.tmp.name)/'live',feed,config={'runtime':{'mode':'LIVE'}})
        with self.assertRaisesRegex(ValueError,'LIVE_EXECUTION_DISABLED'):
            RealVenue(signer=signer,submit=transport,cancel=transport,claim=transport)
        signer.assert_not_called();transport.assert_not_called();self.assertEqual(feed.mock_calls,[])
        self.assertEqual(list((Path(self.tmp.name)/'live').glob('*.sqlite3')),[])

    def test_record_display_cash_is_not_a_funded_account_return(self):
        ledger=HengzhunLedger(self.path,{**fixture.SPACE,'mode':'RECORD'});self.addCleanup(ledger.close)
        ledger.ensure_wallet('W1');state=ledger.snapshot('W1',0)
        self.assertEqual(state['cash_confirmed'],'100');self.assertFalse(state['account_return_valid'])
        self.assertEqual(ledger.events(),[])

    def test_ex003_every_namespace_axis_is_independent_and_shared_wallet_is_not_per_strategy(self):
        spaces=[dict(fixture.SPACE),{**fixture.SPACE,'portfolio_run_id':'OTHER'},
            {**fixture.SPACE,'scenario_id':'S2'},{**fixture.SPACE,'ledger_scope':'SIGNAL_SHADOW'},
            {**fixture.SPACE,'mode':'REPLAY'}]
        ledgers=[]
        for space in spaces:
            ledger=HengzhunLedger(self.path,space);self.addCleanup(ledger.close);ledger.ensure_wallet('W1')
            ledger.reserve(fixture.plan(strategy='A',scope='UNCALIBRATED' if space['ledger_scope']=='SIGNAL_SHADOW' else 'CALIBRATED'),240000)
            with self.assertRaisesRegex(LedgerError,'WALLET_MARKET_OWNER_CONFLICT'):
                ledger.reserve(fixture.plan(strategy='B'),240001)
            self.assertEqual(ledger.snapshot('W1',240001)['cash_reserved'],'5')
            ledgers.append(ledger)
        self.assertEqual(len({x.space for x in ledgers}),5)
        self.assertEqual(self.ledger.conn.execute('SELECT count(*) FROM hz_wallets').fetchone()[0],5)

    def test_ex012_each_presend_version_change_releases_once_and_consumes_market(self):
        for index,(section,key,value) in enumerate((('rules','rule_version','changed'),
            ('trading','tick_size','.001'),('trading','quantity_step','.1'))):
            market='version-'+str(index);ident=self.reserve(market);changed=fixture.contract(market)
            changed[section][key]=value;changed['contract_sha256']=contract_hash(changed)
            with self.assertRaisesRegex(LedgerError,'CONTRACT_CHANGED_BEFORE_SEND'):
                self.venue.submit(ident,changed,240000,clock_bound_ms=0)
            order=self.ledger.intent(ident)
            self.assertEqual((order['state'],order['send_attempts'],order['reserved']),('ABORTED_BEFORE_SEND',0,'0'))
            self.assertEqual(self.ledger.snapshot('W1',240000)['claim_reserved_markets'],0)
            with self.assertRaisesRegex(LedgerError,'MARKET_INTENT_CONSUMED'):self.reserve(market)

    def test_ex013_postsend_contract_change_never_becomes_a_free_cancel(self):
        ident=self.reserve();self.venue.submit(ident,fixture.contract(),240000,clock_bound_ms=0)
        original=self.ledger.intent(ident)['plan'];task=self.venue._task(ident)
        self.assertEqual(task['submitted_request_sha256'],digest(json.loads(task['submitted_request_json'])))
        changed=fixture.book();changed['payload']['contract_sha256']='f'*64
        self.venue.on_event(changed);self.venue.advance(241000)
        state=self.ledger.snapshot('W1',241000)
        self.assertEqual(self.ledger.intent(ident)['state'],'UNKNOWN')
        self.assertEqual((state['cash_reserved'],state['unresolved_max_loss']),('5','5'))
        self.assertIn('POST_SUBMIT_CONTRACT_UNRESOLVED',state['halt_reasons'])
        self.assertEqual(self.ledger.intent(ident)['plan'],original)
        self.assertEqual(self.venue._task(ident)['submitted_request_json'],task['submitted_request_json'])
        self.venue.advance(500000)
        self.assertEqual(self.ledger.intent(ident)['state'],'UNKNOWN')
        # Independent synthetic actual-economics proof can reconcile it once.
        self.ledger.record_match(ident,'actual-proof','.5',fixture.cost('8'),241000)
        self.ledger.confirm_fill('actual-proof',500001);self.ledger.order_terminal(ident,500001,reconciled=True)
        self.assertEqual(self.ledger.snapshot('W1',500001)['cash_confirmed'],'96')
        self.assertEqual(self.ledger.snapshot('W1',500001)['position_cost_unresolved'],'4')
        self.assertIn('POST_SUBMIT_CONTRACT_UNRESOLVED',self.ledger.snapshot('W1',500001)['halt_reasons'])

    def test_receive_clock_batch_is_complete_before_matching_exact_arrival(self):
        ident=self.reserve();self.venue.submit(ident,fixture.contract(),240000,clock_bound_ms=0)
        first=fixture.book(at=241000,price='.6');second=fixture.book(at=241000,price='.5',seq=2)
        self.venue.on_events([first,second]);self.venue.advance(253000)
        self.assertEqual(self.ledger.intent(ident)['state'],'TERMINAL_WITH_FILL')
        self.assertEqual(self.venue._task(ident)['matching_book_event'],second['event_id'])
        self.assertFalse(self.venue.on_events([first,second]))
        self.assertEqual(len(self.ledger.fills(ident)),1)

    def test_late_live_capture_preserves_clocks_and_does_not_halt_without_affected_arrival(self):
        self.venue.advance(241000);event=fixture.book(at=240900)
        outcome=self.venue.ingest_observed_events([event])
        self.assertTrue(outcome['late']);self.assertFalse(outcome['late_arrival_conflict'])
        self.assertEqual(self.venue.clock_ms,241000);self.assertEqual(self.ledger.snapshot('W1',241000)['halt_reasons'],[])
        stored=json.loads(self.ledger.conn.execute('SELECT payload FROM hz_venue_events').fetchone()[0])
        self.assertEqual(stored,event)
        with self.assertRaisesRegex(LedgerError,'RECEIVE_TIME_REGRESSION'):
            self.venue.on_event(fixture.book(at=240950,seq=2))

    def test_late_book_cannot_rewrite_an_already_processed_arrival_or_cash(self):
        ident=self.reserve();self.venue.submit(ident,fixture.contract(),240000,clock_bound_ms=0)
        self.venue.on_event(fixture.book(at=240500,price='.6'));self.venue.advance(241100)
        self.assertEqual(self.ledger.intent(ident)['state'],'TERMINAL_NO_FILL')
        before=self.ledger.snapshot('W1',241100)
        outcome=self.venue.ingest_observed_events([fixture.book(at=240900,price='.5',seq=2)])
        self.assertTrue(outcome['late_arrival_conflict']);self.assertIn('DATA_GAP',self.ledger.snapshot('W1',241100)['halt_reasons'])
        self.assertEqual(self.ledger.fills(ident),[]);self.assertEqual(self.ledger.snapshot('W1',241100)['cash_confirmed'],before['cash_confirmed'])
        self.venue.advance(253000);self.assertEqual(self.ledger.fills(ident),[])

    def test_late_older_book_does_not_halt_when_it_cannot_change_arrival_asof(self):
        ident=self.reserve();self.venue.submit(ident,fixture.contract(),240000,clock_bound_ms=0)
        self.venue.on_event(fixture.book(at=240900,seq=1));self.venue.advance(241100)
        outcome=self.venue.ingest_observed_events([fixture.book(at=240500,price='.6',seq=2)])
        self.assertFalse(outcome['late_arrival_conflict']);self.assertEqual(self.ledger.snapshot('W1',241100)['halt_reasons'],[])

    def test_ex027_committed_fill_survives_real_connection_restart_and_duplicate_delivery(self):
        ident=self.filled();before=self.ledger.snapshot('W1',253000)
        hashes=digest(self.ledger.events());self.ledger.close()
        self.ledger=HengzhunLedger(self.path,dict(fixture.SPACE));self.ledger.recover(254000)
        self.ledger.record_match(ident,'M1-f','.5',fixture.cost(),241000)
        self.ledger.confirm_fill('M1-f',254000);self.ledger.order_terminal(ident,254000,reconciled=True)
        self.assertEqual(self.ledger.snapshot('W1',253000),before);self.assertEqual(digest(self.ledger.events()),hashes)

    def test_ex029_partial_confirmed_plus_unknown_remainder_is_exactly_one_five_dollar_risk(self):
        ident=self.reserve();self.ledger.submit_once(ident,240000);self.ledger.mark_unknown(ident,240001)
        self.ledger.record_match(ident,'partial','.5',fixture.cost('4'),241000);self.ledger.confirm_fill('partial',253000)
        state=self.ledger.snapshot('W1',253000)
        self.assertEqual(self.ledger.intent(ident)['state'],'UNKNOWN')
        self.assertEqual((state['cash_reserved'],state['position_cost_unresolved'],state['unresolved_max_loss']),('3','2','5'))
        self.assertEqual((state['cash_available'],state['open_risk_slots_used'],state['claim_reserved_markets']),('95',1,1))

    def test_ex030_inclusive_time_boundaries_and_arrival_gate(self):
        for index,(at,hold,allowed) in enumerate(((210000,1000,True),(270000,1000,True),(270000,5000,True),
                (270001,1000,False),(270000,6000,False))):
            ledger=HengzhunLedger(self.path,{**fixture.SPACE,'portfolio_run_id':'time-'+str(index)})
            self.addCleanup(ledger.close);ledger.ensure_wallet('W1');contract=fixture.contract(hold=hold)
            plan=fixture.plan();plan['contract_sha256']=contract['contract_sha256'];ident=ledger.reserve(plan,at)['intent_id']
            venue=PaperVenue(ledger,fixture.fee_adapter)
            if allowed:
                venue.submit(ident,contract,at,clock_bound_ms=0);self.assertEqual(ledger.intent(ident)['send_attempts'],1)
            else:
                with self.assertRaises(LedgerError):venue.submit(ident,contract,at,clock_bound_ms=0)
                self.assertEqual(ledger.intent(ident)['send_attempts'],0);self.assertEqual(ledger.intent(ident)['reserved'],'0')
                with self.assertRaisesRegex(LedgerError,'MARKET_INTENT_CONSUMED'):ledger.reserve(plan,at)

    def test_ex034_clock_fault_preserves_existing_position_and_still_allows_settlement(self):
        ident=self.filled()
        self.venue.on_event({'event_id':'clock-fault','boot_id':'test','ingest_seq':1,'received_at':260000,'source_at':260000,
            'source_kind':'OTHER','payload':{'fault_type':'CLOCK_UNKNOWN','wallet_id':'W1'}})
        state=self.ledger.snapshot('W1',260000)
        self.assertEqual((state['cash_confirmed'],state['position_cost_unresolved']),('95','5'))
        self.assertIn('TIME_BOUNDS_UNKNOWN',state['halt_reasons'])
        with self.assertRaisesRegex(LedgerError,'TIME_BOUNDS_UNKNOWN'):self.reserve('blocked',at=260000)
        self.ledger.record_result(fixture.result(),310000);self.venue.advance(370000)
        self.assertEqual(self.ledger.intent(ident)['position_state'],'CLOSED_CLAIMED')
        self.assertEqual(self.ledger.snapshot('W1',370000)['cash_confirmed'],'105')

    def test_ex045_cash_mismatch_after_reservation_also_blocks_send_and_preserves_expected_cash(self):
        ident=self.reserve();self.assertFalse(self.ledger.reconcile_cash('W1','96',240000))
        self.venue.submit(ident,fixture.contract(),240000,clock_bound_ms=0)
        order=self.ledger.intent(ident)
        self.assertEqual((order['state'],order['send_attempts'],order['reserved']),('ABORTED_BEFORE_SEND',0,'0'))
        state=self.ledger.snapshot('W1',240000)
        self.assertEqual(state['cash_confirmed'],'100');self.assertIn('WALLET_RECONCILIATION_MISMATCH',state['halt_reasons'])
        with self.assertRaisesRegex(LedgerError,'WALLET_RECONCILIATION_MISMATCH'):self.reserve('other')

    def test_ex011_missing_economic_metadata_never_sends_in_scenario_or_empirical(self):
        for index,field in enumerate(('rule_version','fee_formula_id','collection_mode','rounding_mode')):
            market='metadata-'+str(index);contract=fixture.contract(market)
            if field=='rule_version':contract['rules'].pop(field)
            else:contract['trading']['fee_contract'].pop(field)
            contract['contract_sha256']=contract_hash(contract);plan=fixture.plan(market)
            plan['contract_sha256']=contract['contract_sha256'];ident=self.ledger.reserve(plan,240000)['intent_id']
            with self.assertRaisesRegex(LedgerError,'CONTRACT_UNVERIFIED'):
                self.venue.submit(ident,contract,240000,clock_bound_ms=0)
            self.assertEqual(self.ledger.intent(ident)['send_attempts'],0)
            self.assertEqual(self.ledger.intent(ident)['reserved'],'0')
        empirical=HengzhunLedger(self.path,{**fixture.SPACE,'portfolio_run_id':'empirical'},
            config={'paper':{'execution_evidence':'EMPIRICAL'}});self.addCleanup(empirical.close)
        with self.assertRaisesRegex(LedgerError,'EMPIRICAL_PROFILE_UNVERIFIED'):
            PaperVenue(empirical,fixture.fee_adapter)
        self.assertEqual(empirical.intents(),[])

    def test_ex039_existing_claim_plus_concurrent_markets_reserves_only_second_slot(self):
        ident=self.filled(quantity='2');self.ledger.record_result(fixture.result(),310000)
        ledgers=[HengzhunLedger(self.path,dict(fixture.SPACE)) for _ in range(2)]
        for ledger in ledgers:self.addCleanup(ledger.close)
        barrier=threading.Barrier(2)
        def reserve(index):
            barrier.wait()
            try:ledgers[index].reserve(fixture.plan('new-'+str(index),strategy='S'+str(index)),310001);return True
            except LedgerError:return False
        with ThreadPoolExecutor(max_workers=2) as pool:self.assertEqual(sum(pool.map(reserve,range(2))),1)
        state=self.ledger.snapshot('W1',310001)
        self.assertEqual((state['claim_pending_markets'],state['claim_reserved_markets'],state['cash_reserved']), (1,1,'5'))
        reasons=self.ledger.guard_reasons(fixture.plan('third'),310002)
        self.assertIn('CLAIM_SLOT_LIMIT',reasons);self.assertIn('WALLET_RISK_SLOT_FULL',reasons)

    def test_ex043_exact_fifteen_percent_high_water_threshold_preserves_position(self):
        ident=self.filled();self.ledger.mark_equity('W1','25',260000)
        self.assertEqual(self.ledger.snapshot('W1',260000)['high_water_nav'],'120')
        self.ledger.mark_equity('W1','7.0001',260001)
        self.assertFalse(self.ledger.snapshot('W1',260001)['drawdown_halt'])
        self.ledger.mark_equity('W1','7',260002);state=self.ledger.snapshot('W1',260002)
        self.assertEqual(state['nav'],'102');self.assertTrue(state['drawdown_halt'])
        self.assertEqual(self.ledger.intent(ident)['position_state'],'OPEN_UNRESOLVED')
        with self.assertRaisesRegex(LedgerError,'DRAWDOWN_HALT'):self.reserve('blocked',at=260003)

    @staticmethod
    def baseline_registration(baseline='B2_PRIOR'):
        return {'baseline_id':baseline,'strategy_id':baseline,'frozen_provenance_sha256':'d'*64,
                'config_sha256':digest({}),'input_manifest_sha256':'e'*64,'registered_at':0}

    def test_replay_baseline_extension_keeps_model_null_and_original_schema_intact(self):
        for baseline in ('B1_FROZEN_UNIFIED','B2_PRIOR'):
            registration=self.baseline_registration(baseline)
            namespace={**fixture.SPACE,'mode':'REPLAY','portfolio_run_id':baseline}
            ledger=HengzhunLedger(self.path,namespace,replay_baseline=registration);self.addCleanup(ledger.close)
            ledger.ensure_wallet('W1');plan=fixture.plan(strategy=baseline,scope='UNCALIBRATED')
            plan.update(config_sha256=digest({}),baseline_registration_sha256=digest(registration))
            ident=ledger.reserve(plan,240000)['intent_id'];intent=ledger.intent(ident)['payload']
            self.assertEqual(intent['record_type'],'BaselineOrderIntent');self.assertIsNone(intent['model_artifact_sha256'])
            validate_record(intent,'baseline_event_contracts.schema.json',definition='BaselineOrderIntent')
            with self.assertRaises(Exception):validate_record(intent,definition='OrderIntent')
            venue=PaperVenue(ledger,fixture.fee_adapter);venue.submit(ident,fixture.contract(),240000,clock_bound_ms=0)
            venue.on_event(fixture.book());venue.advance(253000)
            ledger.record_result(fixture.result(),310000);venue.advance(370000)
            self.assertEqual(ledger.snapshot('W1',370000)['cash_confirmed'],'105')
            self.assertEqual(ledger.snapshot('W1',370000)['replay_baseline'],registration)
            for event in ledger.events():validate_record(event,definition='LedgerEvent')
            for fill in ledger.fills(ident):validate_record(fill['payload'],definition='Fill')

    def test_baseline_policy_cannot_enable_paper_or_relabel_model_or_mutate_existing_run(self):
        registration=self.baseline_registration()
        for mode in ('PAPER','RECORD'):
            with self.assertRaisesRegex(LedgerError,'BASELINE_POLICY_REPLAY_ONLY'):
                HengzhunLedger(self.path,{**fixture.SPACE,'mode':mode},replay_baseline=registration)
        namespace={**fixture.SPACE,'mode':'REPLAY'}
        ledger=HengzhunLedger(self.path,namespace,replay_baseline=registration);self.addCleanup(ledger.close);ledger.ensure_wallet('W1')
        for changes in ({},{'calibration_status':'CALIBRATED'},{'model_artifact_sha256':'a'*64},
                        {'strategy_id':'HENGZHUN_TWAP_VALUE_V1'},{'baseline_registration_sha256':'a'*64}):
            plan=fixture.plan(strategy='B2_PRIOR',scope='UNCALIBRATED')
            plan.update(config_sha256=digest({}),baseline_registration_sha256=digest(registration))
            if not changes:plan.pop('baseline_registration_sha256')
            else:plan.update(changes)
            with self.assertRaisesRegex(LedgerError,'BASELINE_PLAN_PROVENANCE_MISMATCH'):ledger.reserve(plan,240000)
        with self.assertRaisesRegex(LedgerError,'BASELINE_POLICY_IMMUTABLE_NEW_RUN_REQUIRED'):
            HengzhunLedger(self.path,namespace,replay_baseline={**registration,'input_manifest_sha256':'f'*64})
        with self.assertRaisesRegex(LedgerError,'BASELINE_POLICY_IMMUTABLE_NEW_RUN_REQUIRED'):
            HengzhunLedger(self.path,namespace)
        with self.assertRaisesRegex(LedgerError,'BASELINE_CONFIG_MISMATCH'):
            HengzhunLedger(self.path,{**namespace,'portfolio_run_id':'bad-config'},config={'different':True},replay_baseline=registration)
        future={**registration,'registered_at':240001}
        later=HengzhunLedger(self.path,{**namespace,'portfolio_run_id':'future'},replay_baseline=future)
        self.addCleanup(later.close);later.ensure_wallet('W1')
        plan=fixture.plan(strategy='B2_PRIOR',scope='UNCALIBRATED')
        plan.update(config_sha256=digest({}),baseline_registration_sha256=digest(future))
        with self.assertRaisesRegex(LedgerError,'BASELINE_NOT_YET_REGISTERED'):later.reserve(plan,240000)
