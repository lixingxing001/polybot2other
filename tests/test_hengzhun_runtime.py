"""运行器隔离/因果验收；所有市场、价格和余额均为人工夹具。"""
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from polybot2other.hengzhun.runtime import HengzhunRuntime
from polybot2other.hengzhun.contracts import contract_hash, digest
from polybot2other.hengzhun.config import load_config
from test_hengzhun_execution import contract, plan, result


@dataclass
class Market:
    round_id:str='M1'
    condition_id:str='M1'
    slug:str='M1'
    started_at:float=0
    ends_at:float=300
    up_token:str='M1-UP'
    down_token:str='M1-DOWN'
    target_price:float=100
    question:str='synthetic'


class Clock:
    def snapshot(self,*args):return {'offset_bound_ms':0,'status':'VERIFIED'}


class Feed:
    def __init__(self):
        self.market=Market();self.references=SimpleNamespace(observer=None);self.books=SimpleNamespace(observer=None)
        self.research_observer=None
    def observation(self):return self.market,None,None


def synthetic_prediction(r):
    prior={'spot':'100','baseline':'100','known_integral_usd_seconds':'0','known_seconds':0,
        'mean_unadjusted_usd':'100','mean_adjusted_usd':'100','variance_rate_usd2_per_second':'1',
        'process_variance_usd2':'20','total_variance_usd2':'21','proxy_bias_bps':0.,'proxy_sd_bps':1.,
        'proxy_sample_count':120,'proxy_p95_abs_error_bps':1.,'proxy_status':'READY',
        'fast_return_count':24,'slow_return_count':120,'volatility_span_seconds':600,
        'p_raw_up':.8,'p_raw_down':.2}
    fitted={'model_artifact_sha256':digest(r.model),'model_version':'SYNTHETIC_ORCHESTRATION_ONLY',
        'feature_order':['u','v','u_times_h'],'features':[1.,0.,0.],'standardized_features':[1.,0.,0.],
        'p_fit_up':.85,'p10_up':.82,'p90_up':.9,'guard_up':.8,'guard_down':.08,
        'bootstrap_success_count':100,'inference_success_count':100,'model_available_at':0,'model_expires_at':1000000}
    return prior,fitted


ACCEPTANCE_CASES={'EX-002':'test_fully_qualified_calibrated_signal_in_record_has_no_economic_writes','EX-004':'test_paper_cold_start_keeps_qualified_unfitted_signal_out_of_account'}

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.feed=Feed();self.now=240000

    def runtime(self,mode='RECORD',verified=True):
        if verified:
            directory=self.root/'verified-contracts';directory.mkdir(exist_ok=True)
            (directory/'M1.json').write_text(json.dumps(contract()))
        runtime=HengzhunRuntime(self.root,self.feed,config={'runtime':{'mode':mode}},clock=Clock(),event_clock=lambda:self.now)
        self.addCleanup(runtime.journal.conn.close);self.addCleanup(runtime.ledger.close)
        runtime.contract=runtime._contract(self.feed.market,self.now)
        return runtime

    @staticmethod
    def book(runtime,at,side='UP',ask='.50'):
        runtime.ingest('BOOK',{'event_type':'book','timestamp':str(at),'asset_id':'M1-'+side,
            'bids':[{'price':'.49','size':'100'}],'asks':[{'price':ask,'size':'100'}]},at)

    @staticmethod
    def reference(runtime,at,value=100,channel='price.crypto'):
        body={'symbol':'btcusd','source':'chainlink','timestamp':at,'value':value}
        if channel.endswith('twap'):body['window_seconds']=60
        runtime.ingest('REFERENCE',{'channel':channel,'payload':body},at)

    def reserve(self,runtime):
        value=plan(wallet='HENGZHUN');value['config_sha256']=runtime.config_sha
        value['contract_sha256']=runtime.contract['contract_sha256']
        return runtime.ledger.reserve(value,240000)['intent_id']

    def test_record_records_raw_and_feature_but_no_economic_events(self):
        r=self.runtime();self.reference(r,240000);self.reference(r,240000,channel='price.crypto.twap')
        self.book(r,240000);self.book(r,240000,'DOWN');r.tick(240000)
        self.assertGreater(r.journal.counts()['raw_events'],4);self.assertEqual(r.journal.counts()['features'],1)
        self.assertEqual(r.ledger.intents(),[]);self.assertEqual(r.ledger.events(),[])
        self.assertEqual(r.last_frame['decision'],'RECORD_ONLY')
        self.assertIn('MODEL_UNFITTED',r.last_frame['decision_reasons'])
        self.assertEqual(r.ledger.snapshot('HENGZHUN',240000)['cash_confirmed'],'100')

    def test_unverified_missing_baseline_records_reason_without_runtime_crash(self):
        self.feed.market.target_price=0;r=self.runtime(verified=False)
        self.reference(r,240000);r.tick(240000)
        self.assertIn('BASELINE_UNVERIFIED',r.last_frame['data_reasons'])
        self.assertEqual(r.ledger.intents(),[])

    def test_repeat_source_receipt_cannot_refresh_stale_price(self):
        r=self.runtime();self.reference(r,240000);self.reference(r,240000)
        self.assertEqual(r.reference['spot']['received_at'],240000)
        r.ingest('REFERENCE',{'channel':'price.crypto','payload':{'symbol':'btcusd','source':'chainlink','timestamp':240000,'value':100}},245000)
        self.assertEqual(r.reference['spot']['received_at'],240000)
        self.assertEqual(len(r.journal.prices('price.crypto',0,300000,300000)),1)

    def test_late_source_cannot_repair_frozen_five_second_grid(self):
        from polybot2other.hengzhun.model import causal_grid
        r=self.runtime();r.ingest('REFERENCE',{'channel':'price.crypto','payload':{
            'symbol':'btcusd','source':'chainlink','timestamp':239500,'value':100}},240100)
        self.assertIsNone(causal_grid(r.journal.prices('price.crypto',230000,250000,250000),240000,240000)[0].value)

    def test_normalized_book_reaches_venue_and_confirmation_uses_match_clock(self):
        r=self.runtime('PAPER');ident=self.reserve(r);r.venue.submit(ident,r.contract,240000,clock_bound_ms=0)
        self.book(r,240500);self.now=241000;r.tick(self.now)
        self.assertEqual(r.ledger.fills(ident)[0]['state'],'MATCHED')
        self.feed.market=None;self.now=252999;r.tick(self.now)
        self.assertEqual(r.ledger.fills(ident)[0]['state'],'MATCHED')
        self.now=253000;r.tick(self.now)
        self.assertEqual(r.ledger.fills(ident)[0]['state'],'CONFIRMED')
        self.assertEqual(r.ledger.snapshot('HENGZHUN',253000)['cash_confirmed'],'95')

    def test_verified_result_and_claim_continue_without_current_market(self):
        r=self.runtime('PAPER');ident=self.reserve(r);r.venue.submit(ident,r.contract,240000,clock_bound_ms=0)
        self.book(r,240500);self.now=253000;r.tick(self.now)
        self.feed.market=None;r.ingest('RESULT',result(at=310000),310000)
        self.assertEqual(r.ledger.intent(ident)['position_state'],'CLAIM_PENDING')
        self.now=370000;r.tick(self.now)
        self.assertEqual(r.ledger.intent(ident)['position_state'],'CLOSED_CLAIMED')
        self.assertEqual(r.ledger.snapshot('HENGZHUN',370000)['cash_confirmed'],'105')
        self.assertEqual(r.journal.counts()['official_results'],1)

    def test_record_result_repeated_does_not_move_label_available_at(self):
        r=self.runtime();r.ingest('RESULT',result(at=310000),310000)
        duplicate=result(at=310000);duplicate['received_at']=duplicate['verified_at']=duplicate['label_available_at']=320000
        r.ingest('RESULT',duplicate,320000)
        row=r.journal.conn.execute('SELECT available_at FROM official_results').fetchone()
        self.assertEqual(row[0],310000);self.assertEqual(r.journal.counts()['official_results'],1)

    def test_rule_compatibility_hash_excludes_period_identity_and_baseline(self):
        c=contract();d=contract('M2');d['baseline']['value']='110'
        self.assertEqual(HengzhunRuntime._rule_hash(c),HengzhunRuntime._rule_hash(d))
        d['rules']['rule_version']='v2'
        self.assertNotEqual(HengzhunRuntime._rule_hash(c),HengzhunRuntime._rule_hash(d))

    def test_replay_requires_injected_event_clock_and_forbids_network_start(self):
        with self.assertRaisesRegex(ValueError,'REPLAY_REQUIRES_OFFLINE_EVENT_CLOCK'):
            HengzhunRuntime(self.root,self.feed,config={'runtime':{'mode':'REPLAY'}})
        r=self.runtime('REPLAY')
        with self.assertRaisesRegex(ValueError,'REPLAY_NETWORK_START_FORBIDDEN'):r.start()
        self.assertIsNone(self.feed.references.observer)

    def test_late_landmarks_missing_once_and_never_backfilled(self):
        r=self.runtime();r.tick(240000);r.tick(270000)
        rows=r.journal.conn.execute('SELECT target,status FROM landmarks ORDER BY target').fetchall()
        self.assertEqual(len(rows),5);self.assertTrue(all(row['status']=='MISSING_OR_TOO_LATE' for row in rows))
        before=r.journal.counts()['landmarks'];r.tick(270250)
        self.assertEqual(r.journal.counts()['landmarks'],before)

    def test_presend_contract_change_aborts_original_plan(self):
        r=self.runtime('PAPER');p=plan(wallet='HENGZHUN');p['contract_sha256']='a'*64
        self.assertFalse(r._pre_send_recheck(p,240000)['allow'])

    def test_snapshot_is_research_only_and_does_not_read_live_wallet(self):
        r=self.runtime();status=r.snapshot()
        self.assertFalse(status['live_execution_enabled']);self.assertEqual(status['mode'],'RECORD')
        self.assertEqual(status['model_status'],'UNFITTED');self.assertEqual(status['research_status'],'INSUFFICIENT')
        self.assertEqual(status['wallet']['cash_confirmed'],'100')

    def test_frozen_decision_contains_accepted_intent_and_preserves_plan(self):
        r=self.runtime('PAPER');self.reference(r,240000);self.reference(r,240000,channel='price.crypto.twap')
        self.book(r,240000);self.book(r,240000,'DOWN');r.healthy_since=230000
        # Synthetic predictive doubles exercise orchestration, never a trained artifact.
        r.model={'artifact_status':'SYNTHETIC_ORCHESTRATION_ONLY'};r._last_model_market='M1'
        prior,fitted=synthetic_prediction(r)
        with patch.object(r,'_prior',return_value=(prior,SimpleNamespace(calibrated=True))),patch.object(r,'_fitted',return_value=fitted):
            r.tick(240000)
        self.assertEqual(r.last_frame['decision'],'INTENT_CREATED')
        persisted=json.loads(r.journal.conn.execute('SELECT json FROM features').fetchone()[0])
        self.assertEqual(persisted,r.last_frame)
        order=r.ledger.intent(persisted['intent_id']);self.assertEqual(order['send_attempts'],1)
        self.assertEqual(order['plan']['decision_frame_id'],persisted['frame_id'])
        self.assertEqual(order['plan']['side'],'UP')

    def test_processing_delay_does_not_permanently_halt_unaffected_future_orders(self):
        r=self.runtime('PAPER');r.venue.advance(240100)
        self.book(r,240050)
        self.assertIsNone(r.fault)
        self.assertNotIn('DATA_GAP',r.ledger.snapshot('HENGZHUN',240100)['halt_reasons'])
        self.assertEqual(r.venue.clock_ms,240100)
        self.assertEqual(r.books.asof('M1-UP',240100)[0]['received_at'],240050)

    def test_late_reconstruction_conflict_never_retroactively_changes_fill(self):
        r=self.runtime('PAPER');ident=self.reserve(r);r.venue.submit(ident,r.contract,240000,clock_bound_ms=0)
        self.book(r,240500,ask='.50');r.venue.advance(241000)
        before=r.ledger.fills(ident)
        self.now=241100;self.book(r,240800,ask='.60')
        self.assertEqual(r.ledger.fills(ident),before)
        self.assertEqual(r.fault,'RECEIVE_ORDER_UNCERTAIN')
        self.assertIn('DATA_GAP',r.ledger.snapshot('HENGZHUN',241100)['halt_reasons'])

    def test_transient_recovery_needs_full_sync_and_ten_seconds_and_keeps_drawdown(self):
        r=self.runtime('PAPER');r.fault='QUEUE_OVERFLOW';r.recovery_fault_at=240000
        r.ledger.halt('HENGZHUN','DATA_GAP',240000);r.ledger.halt('HENGZHUN','DRAWDOWN_HALT',240000)
        r._recover_transient_data(r.contract,250000,[]);self.assertEqual(r.fault,'QUEUE_OVERFLOW')
        r.full_resync_at={key:250000 for key in ('price.crypto','price.crypto.twap','M1-UP','M1-DOWN')}
        r._recover_transient_data(r.contract,250000,[]);r._recover_transient_data(r.contract,259999,[])
        self.assertEqual(r.fault,'QUEUE_OVERFLOW')
        r._recover_transient_data(r.contract,260000,[])
        self.assertIsNone(r.fault)
        reasons=r.ledger.snapshot('HENGZHUN',260000)['halt_reasons']
        self.assertNotIn('DATA_GAP',reasons);self.assertIn('DRAWDOWN_HALT',reasons)
        self.assertEqual(r.healthy_since,250000)  # Do not start a second ten-second warmup.

    def test_each_boot_has_immutable_code_and_config_manifest(self):
        r=self.runtime();files=list((self.root/'boots').glob('*.json'));self.assertEqual(len(files),1)
        value=json.loads(files[0].read_text())
        self.assertEqual(value['code_manifest_sha256'],r.code_sha);self.assertEqual(value['config_sha256'],r.config_sha)
        self.assertEqual(value['boot_id'],r.journal.boot_id)

    def test_stop_drains_already_received_queue_and_marks_capture_boundary(self):
        r=self.runtime();r.capture('CLOCK',{'status':'TEST'},240000);self.assertEqual(r.pending.qsize(),1)
        r.stop();self.assertEqual(r.pending.qsize(),0)
        self.assertEqual(r.journal.conn.execute("SELECT count(*) FROM quality_events WHERE code='CAPTURE_STOPPED'").fetchone()[0],1)

    def test_fully_qualified_calibrated_signal_in_record_has_no_economic_writes(self):
        r=self.runtime('RECORD');self.reference(r,240000);self.reference(r,240000,channel='price.crypto.twap')
        self.book(r,240000);self.book(r,240000,'DOWN');r.healthy_since=230000
        r.model={'artifact_status':'SYNTHETIC_ORCHESTRATION_ONLY'};r._last_model_market='M1'
        prior,fitted=synthetic_prediction(r)
        with patch.object(r,'_prior',return_value=(prior,SimpleNamespace(calibrated=True))),patch.object(r,'_fitted',return_value=fitted):r.tick(240000)
        self.assertEqual(r.last_frame['calibration_status'],'CALIBRATED')
        self.assertEqual(r.last_frame['landmark_target_seconds'],60);self.assertEqual(r.last_frame['landmark_status'],'PRESENT')
        frozen=json.loads(r.journal.conn.execute('SELECT json FROM features').fetchone()[0])
        self.assertEqual(frozen['landmark_status'],'PRESENT');self.assertEqual(frozen['landmark_target_seconds'],60)
        self.assertTrue(r.last_frame['candidates']);self.assertEqual(r.last_frame['data_status'],'READY')
        self.assertEqual(r.last_frame['decision'],'RECORD_ONLY');self.assertEqual(r.ledger.intents(),[]);self.assertEqual(r.ledger.events(),[])

    def test_paper_cold_start_keeps_qualified_unfitted_signal_out_of_account(self):
        r=self.runtime('PAPER');self.reference(r,240000);self.reference(r,240000,channel='price.crypto.twap')
        self.book(r,240000);self.book(r,240000,'DOWN');r.healthy_since=230000
        prior,_=synthetic_prediction(r)
        with patch.object(r,'_prior',return_value=(prior,SimpleNamespace(calibrated=True))):r.tick(240000)
        self.assertEqual(r.last_frame['data_status'],'READY');self.assertTrue(r.last_frame['candidates'])
        self.assertIn('MODEL_UNFITTED',r.last_frame['decision_reasons']);self.assertEqual(r.ledger.intents(),[])
        self.assertEqual([e['kind'] for e in r.ledger.events()],['INITIAL_CASH'])

    def test_capture_batch_keeps_full_raw_sequence_without_drops(self):
        r=self.runtime()
        for i in range(10):r.capture('CLOCK',{'index':i},240000+i)
        self.assertEqual(r._drain_capture_batch(limit=3,max_seconds=1),3)
        self.assertEqual(r.pending.qsize(),7)
        self.assertEqual(r._drain_capture_batch(limit=20,max_seconds=1),7)
        records=[json.loads(row[0]) for row in r.journal.conn.execute("SELECT json FROM raw_events WHERE kind='CLOCK' ORDER BY seq")]
        self.assertEqual([event['payload']['index'] for event in records],list(range(10)))
        self.assertEqual([event['received_at'] for event in records],list(range(240000,240010)))
        self.assertEqual(r.drop_count,0)

    def test_raw_batch_is_durable_before_paper_venue_consequences(self):
        r=self.runtime('PAPER');ident=self.reserve(r);r.venue.submit(ident,r.contract,240000,clock_bound_ms=0)
        event={'event_type':'book','timestamp':'240500','asset_id':'M1-UP',
               'bids':[{'price':'.49','size':'100'}],'asks':[{'price':'.50','size':'100'}]}
        r.capture('BOOK',event,240500)
        original=r.venue.ingest_observed_events;seen=[]
        def checked(events):
            self.assertFalse(r.journal.conn.in_transaction)
            with __import__('sqlite3').connect(r.path/'events.sqlite3') as other:
                self.assertEqual(other.execute("SELECT count(*) FROM raw_events WHERE kind='ORDER_BOOK'").fetchone()[0],1)
            seen.extend(events);return original(events)
        with patch.object(r.venue,'ingest_observed_events',checked):r._drain_capture_batch(limit=10,max_seconds=1)
        self.assertEqual(len(seen),1);self.assertEqual(r.venue.clock_ms,240500)

    def test_raw_batch_rollback_retains_every_capture_for_retry(self):
        r=self.runtime()
        for i in range(5):r.capture('CLOCK',{'index':i},240000+i)
        original=r.journal.append;attempt=[0]
        def fail_once(*args,**kwargs):
            attempt[0]+=1
            if attempt[0]==3:raise OSError('SYNTHETIC_DISK_FAILURE')
            return original(*args,**kwargs)
        with patch.object(r.journal,'append',fail_once):
            with self.assertRaises(OSError):r._drain_capture_batch(limit=5,max_seconds=1)
        self.assertEqual(len(r._retry_items)+r.pending.qsize(),5)
        self.assertEqual(r.journal.conn.execute("SELECT count(*) FROM raw_events WHERE kind='CLOCK'").fetchone()[0],0)
        r._drain_capture_batch(limit=5,max_seconds=1)
        self.assertEqual(r.journal.conn.execute("SELECT count(*) FROM raw_events WHERE kind='CLOCK'").fetchone()[0],5)
        self.assertEqual(r.drop_count,0)

    def test_worker_backlog_is_visible_and_does_not_starve_landmark_decisions(self):
        r=self.runtime();r.last_decision_at=0
        for i in range(300):r.capture('CLOCK',{'index':i},230000+i)
        r._worker_cycle()
        self.assertGreater(r.pending.qsize(),0);self.assertEqual(r.last_heartbeat,240000)
        self.assertIn('PROCESSING_BACKLOG',r.last_frame['data_reasons'])
        self.assertEqual(r.last_frame['landmark_status'],'MISSING_OR_TOO_LATE')
        self.assertEqual(r.ledger.intents(),[])

    def test_busy_stop_preserves_queue_in_fsynced_immutable_raw_spool(self):
        import hashlib
        r=self.runtime()
        for i in range(5):r.capture('CLOCK',{'index':i},240000+i,raw_text='raw-'+str(i))
        r.thread=SimpleNamespace(join=lambda **kwargs:None,is_alive=lambda:True)
        r.stop()
        manifests=list((r.path/'capture-spool').glob('*.manifest.json'));self.assertEqual(len(manifests),1)
        manifest=json.loads(manifests[0].read_text());file=r.path/'capture-spool'/manifest['file']
        records=[json.loads(line) for line in file.read_text().splitlines()]
        self.assertEqual(manifest['event_count'],5);self.assertEqual(manifest['sha256'],hashlib.sha256(file.read_bytes()).hexdigest())
        self.assertEqual([record['raw_text'] for record in records],['raw-'+str(i) for i in range(5)])
        self.assertEqual(r.pending.qsize(),0);self.assertEqual(r.drop_count,0)

    def test_result_polling_reaches_old_market_beyond_last_twelve(self):
        r=self.runtime()
        for index in range(2,24):
            value=contract('M'+str(index));r.markets[value['market']['condition_id']]=value;r.journal.store_contract(value)
        chosen=[condition for condition,_ in r._result_poll_candidates(310000)]
        self.assertIn('M1',chosen);self.assertEqual(len(chosen),2)
        for condition in chosen:r._mark_result_poll_attempt(condition,310000)
        following=[condition for condition,_ in r._result_poll_candidates(310001)]
        self.assertFalse(set(chosen)&set(following));self.assertEqual(len(following),2)

    def test_result_polling_prioritizes_unresolved_position_and_preserves_fair_slot(self):
        r=self.runtime('PAPER')
        for index in range(2,20):
            value=contract('M'+str(index));r.markets[value['market']['condition_id']]=value;r.journal.store_contract(value)
        ident=self.reserve(r);r.ledger.submit_once(ident,240000);r.ledger.mark_unknown(ident,240001)
        r._mark_result_poll_attempt('M1',310000)
        selected=[condition for condition,_ in r._result_poll_candidates(340000)]
        self.assertEqual(selected[0],'M1');self.assertNotEqual(selected[1],'M1')
        for condition in selected:r._mark_result_poll_attempt(condition,340000)
        self.assertNotIn('M1',[condition for condition,_ in r._result_poll_candidates(340001)])

    def test_result_poll_cooldown_survives_restart(self):
        r=self.runtime();r._mark_result_poll_attempt('M1',310000)
        second=HengzhunRuntime(self.root,self.feed,clock=Clock(),event_clock=lambda:310001)
        self.addCleanup(second.journal.conn.close);self.addCleanup(second.ledger.close)
        self.assertEqual(second._result_poll_candidates(310001),[])
        self.assertEqual(second._result_poll_candidates(340000)[0][0],'M1')
        state=second.journal.conn.execute('SELECT attempts,last_polled_at FROM result_poll_state WHERE condition_id=?',('M1',)).fetchone()
        self.assertEqual((state['attempts'],state['last_polled_at']),(1,310000))

    def test_completed_raw_final_does_not_poll_forever_without_verified_contract(self):
        r=self.runtime(verified=False)
        r.ingest('RESULT',{'condition_id':'M1','result':{'outcome':'Up','target_price':100,'final_price':101}},310000)
        for at in (310000,1000000,100000000):self.assertEqual(r._result_poll_candidates(at),[])
        state=r.journal.conn.execute('SELECT completion_status FROM result_poll_state WHERE condition_id=?',('M1',)).fetchone()
        self.assertEqual(state[0],'RAW_FINAL_OBSERVED')
        # A later verified contract must reopen raw-only completion for a verified label.
        r.markets['M1']=contract()
        self.assertEqual(r._result_poll_candidates(100000001)[0][0],'M1')

    def test_completed_verified_result_does_not_poll_again_after_restart(self):
        r=self.runtime();r.ingest('RESULT',result(at=310000),310000)
        self.assertEqual(r._result_poll_candidates(310000),[])
        second=HengzhunRuntime(self.root,self.feed,clock=Clock(),event_clock=lambda:900000)
        self.addCleanup(second.journal.conn.close);self.addCleanup(second.ledger.close)
        self.assertEqual(second._result_poll_candidates(900000),[])
        self.assertEqual(second.journal.conn.execute("SELECT completion_status FROM result_poll_state WHERE condition_id='M1'").fetchone()[0],'VERIFIED_FINAL')

    def test_result_response_only_queued_is_not_marked_completed(self):
        r=self.runtime();r._mark_result_poll_attempt('M1',310000)
        r.capture('RESULT',{'condition_id':'M1','result':{'outcome':'Up','target_price':100,'final_price':101}},310000)
        self.assertEqual(r._result_poll_candidates(340000)[0][0],'M1')
        r._drain_capture_batch(limit=1,max_seconds=1)
        self.assertEqual(r._result_poll_candidates(340001),[])
