"""Qualified synthetic frames use independent shadow execution, never pooled NAV."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from polybot2other.hengzhun.contracts import digest,validate_record
from polybot2other.hengzhun.runtime import HengzhunRuntime
from polybot2other.hengzhun.shadow import SignalShadowCoordinator
import test_hengzhun_runtime as runtime_fixture
import test_hengzhun_execution as execution_fixture


class SignalShadowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.feed=runtime_fixture.Feed();self.now=240000
        directory=self.root/'runtime'/'verified-contracts';directory.mkdir(parents=True)
        self.contract=execution_fixture.contract();(directory/'M1.json').write_text(json.dumps(self.contract))
        self.runtime=HengzhunRuntime(self.root/'runtime',self.feed,config={'runtime':{'mode':'PAPER'}},
            clock=runtime_fixture.Clock(),event_clock=lambda:self.now)
        if self.runtime.shadow:self.runtime.shadow.close();self.runtime.shadow=None
        self.addCleanup(self.runtime.journal.conn.close);self.addCleanup(self.runtime.ledger.close)
        self.runtime.contract=self.runtime._contract(self.feed.market,self.now)
        self.shadow=SignalShadowCoordinator(self.root/'shadow.sqlite3',self.runtime.namespace,self.runtime.config)
        self.addCleanup(lambda:self.shadow.close())
        for channel in ('price.crypto','price.crypto.twap'):
            runtime_fixture.RuntimeTests.reference(self.runtime,240000,channel=channel)
        for side in ('UP','DOWN'):runtime_fixture.RuntimeTests.book(self.runtime,240000,side)
        self.runtime.healthy_since=230000
        self.runtime.model={'artifact_status':'SYNTHETIC_ORCHESTRATION_ONLY'};self.runtime._last_model_market='M1'
        prior={'spot':'100','baseline':'100','known_integral_usd_seconds':'0','known_seconds':0,
            'mean_unadjusted_usd':'100','mean_adjusted_usd':'100','variance_rate_usd2_per_second':'1',
            'process_variance_usd2':'20','total_variance_usd2':'21','proxy_bias_bps':0.,'proxy_sd_bps':1.,
            'proxy_sample_count':120,'proxy_p95_abs_error_bps':1.,'proxy_status':'READY',
            'fast_return_count':24,'slow_return_count':120,'volatility_span_seconds':600,
            'p_raw_up':.8,'p_raw_down':.2}
        fitted={'model_artifact_sha256':digest(self.runtime.model),'model_version':'SYNTHETIC_ORCHESTRATION_ONLY',
            'feature_order':['u','v','u_times_h'],'features':[1.,0.,0.],'standardized_features':[1.,0.,0.],
            'p_fit_up':.85,'p10_up':.82,'p90_up':.9,'guard_up':.8,'guard_down':.08,
            'bootstrap_success_count':100,'inference_success_count':100,'model_available_at':0,'model_expires_at':1000000}
        with patch.object(self.runtime,'_prior',return_value=(prior,SimpleNamespace(calibrated=True))),\
             patch.object(self.runtime,'_fitted',return_value=fitted):self.runtime.tick(240000)
        self.frame=deepcopy(self.runtime.last_frame);validate_record(self.frame,definition='FeatureFrame')
        self.assertEqual(self.frame['decision'],'INTENT_CREATED')

    @staticmethod
    def allow(plan,at):
        return {**{k:plan[k] for k in ('side','limit_price','quantity_shares','contract_sha256','config_sha256','model_artifact_sha256')},'allow':True}

    def consider(self,frame=None,callback=None):
        return self.shadow.consider(frame or self.frame,self.contract,240000,clock_bound_ms=0,
            pre_send_recheck=callback or self.allow)

    def test_qualified_shadow_executes_despite_full_portfolio_and_never_changes_its_ledger(self):
        before=digest(self.runtime.ledger.events());state=self.runtime.ledger.snapshot('HENGZHUN',240000)
        self.assertEqual(state['open_risk_slots_used'],1)
        ident=self.consider();self.assertIsNotNone(ident)
        self.assertIsNone(self.consider())
        order=self.shadow.ledger.intent(ident);self.assertEqual(order['send_attempts'],1)
        self.assertEqual(order['payload']['identity']['economic_space']['ledger_scope'],'SIGNAL_SHADOW')
        self.assertEqual(self.shadow.ledger.snapshot(order['wallet_id'],240000)['initial_cash'],'5')
        self.assertFalse(self.shadow.ledger.snapshot(order['wallet_id'],240000)['account_return_valid'])
        self.shadow.on_event(execution_fixture.book());self.shadow.advance(253000)
        self.assertEqual(self.shadow.ledger.fills(ident)[0]['state'],'CONFIRMED')
        result=execution_fixture.result()
        self.shadow.on_event({'event_id':'result','boot_id':'test','ingest_seq':2,'received_at':310000,
            'source_at':300000,'source_kind':'OFFICIAL_RESULT','payload':result})
        self.shadow.advance(370000)
        self.assertEqual(self.shadow.ledger.intent(ident)['position_state'],'CLOSED_CLAIMED')
        report=self.shadow.snapshot(370000)
        self.assertEqual(report['filled_markets'],1);self.assertEqual(report['settled_markets'],1)
        self.assertFalse(report['account_return_valid']);self.assertNotIn('return_pct',report)
        self.assertEqual(report['net_pnl'],report['hypothetical_pnl'])
        self.assertEqual(digest(self.runtime.ledger.events()),before)
        frame=json.loads(self.shadow.ledger.conn.execute('SELECT json FROM hz_shadow_frames').fetchone()[0])
        validate_record(frame,definition='FeatureFrame')
        self.assertEqual(frame['identity']['wallet_id'],order['wallet_id'])

    def test_record_uncalibrated_and_proxy_warmup_never_create_economic_shadow(self):
        variants=[]
        uncalibrated=deepcopy(self.frame);uncalibrated.update(calibration_status='UNCALIBRATED',fitted=None)
        variants.append(uncalibrated)
        warming=deepcopy(self.frame);warming['prior']['proxy_status']='WARMUP_FLOOR';variants.append(warming)
        missing=deepcopy(self.frame);missing['data_reasons']=['REFERENCE_MISSING'];variants.append(missing)
        for frame in variants:self.assertIsNone(self.consider(frame))
        self.assertEqual(self.shadow.ledger.intents(),[]);self.assertEqual(self.shadow.ledger.events(),[])
        record=SignalShadowCoordinator(self.root/'record.sqlite3',{**self.runtime.namespace,'mode':'RECORD'},self.runtime.config)
        self.addCleanup(record.close)
        self.assertIsNone(record.consider(self.frame,self.contract,240000,clock_bound_ms=0,pre_send_recheck=self.allow))
        self.assertEqual(record.ledger.intents(),[]);self.assertEqual(record.ledger.events(),[])

    def test_fresh_recheck_abort_consumes_market_without_send_or_retry(self):
        ident=self.consider(callback=lambda plan,at:{'allow':False});order=self.shadow.ledger.intent(ident)
        self.assertEqual((order['state'],order['reserved'],order['send_attempts']),('ABORTED_BEFORE_SEND','0',0))
        self.assertIsNone(self.consider());self.assertEqual(len(self.shadow.ledger.intents()),1)

    def test_each_market_has_its_own_single_intent_wallet_without_a_pooled_return(self):
        first=self.consider();contract=execution_fixture.contract('M2');frame=deepcopy(self.frame)
        frame.update(frame_id=digest(['second',frame['frame_id']]),decision_id='second-decision',
            market=contract['market'],contract_sha256=contract['contract_sha256'])
        frame['up_book']['asset_id']='M2-UP';frame['down_book']['asset_id']='M2-DOWN'
        for candidate in frame['candidates']:candidate['asset_id']='M2-'+candidate['side']
        validate_record(frame,definition='FeatureFrame')
        second=self.shadow.consider(frame,contract,240000,clock_bound_ms=0,pre_send_recheck=self.allow)
        self.assertNotEqual(self.shadow.ledger.intent(first)['wallet_id'],self.shadow.ledger.intent(second)['wallet_id'])
        self.assertEqual(len(self.shadow.ledger.intents()),2)
        for order in self.shadow.ledger.intents():
            snapshot=self.shadow.ledger.snapshot(order['wallet_id'],240000)
            self.assertEqual(snapshot['initial_cash'],'5');self.assertEqual(snapshot['open_risk_slots_used'],1)
        report=self.shadow.snapshot(240000)
        self.assertFalse(report['account_return_valid']);self.assertNotIn('total_equity',report)
        self.assertEqual(report['capital_model'],'INDEPENDENT_FIXED_BUDGET_PER_MARKET')

    def test_callback_exception_safely_aborts_the_provably_unsent_reservation(self):
        def fail(plan,at):raise ValueError('fresh source unavailable')
        with self.assertRaisesRegex(ValueError,'fresh source'):self.consider(callback=fail)
        order=self.shadow.ledger.intents()[0]
        self.assertEqual((order['state'],order['reserved'],order['send_attempts']),('ABORTED_BEFORE_SEND','0',0))
        self.assertIsNone(self.consider())

    def test_shadow_restart_recovers_reserved_and_never_resends_submitting(self):
        ident=self.consider();self.shadow.ledger.mark_unknown(ident,240001)
        self.shadow.close();self.shadow=SignalShadowCoordinator(self.root/'shadow.sqlite3',self.runtime.namespace,self.runtime.config)
        self.shadow.recover(240002)
        self.assertEqual(self.shadow.ledger.intent(ident)['state'],'UNKNOWN');self.assertIsNone(self.consider())
        self.assertEqual(self.shadow.ledger.intent(ident)['send_attempts'],1)

    def test_invalid_feature_contract_rolls_back_before_shadow_intent(self):
        frame=deepcopy(self.frame);frame['fitted']['guard_up']=float('inf')
        with self.assertRaises(Exception):self.consider(frame)
        self.assertEqual(self.shadow.ledger.intents(),[]);self.assertEqual(self.shadow.ledger.events(),[])


if __name__=='__main__':unittest.main()
