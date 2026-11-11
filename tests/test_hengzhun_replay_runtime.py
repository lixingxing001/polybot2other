"""完整策略重放的人工事件验收；不使用或声称存在真实训练数据。"""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from polybot2other.hengzhun.data import EventJournal
from polybot2other.hengzhun.runtime import HengzhunRuntime
from polybot2other.hengzhun.replay_runtime import run_strategy_scenarios
from polybot2other.hengzhun.contracts import digest
from test_hengzhun_execution import contract


ACCEPTANCE_CASES={'EX-006':'test_full_strategy_future_tails_do_not_rewrite_past_actions_or_ledger','EX-050':'test_cold_paper_replay_all_scenarios_independent_and_unfitted'}

class ReplayRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.path=Path(self.temp.name)
        self.journal=EventJournal(self.path/'source.sqlite3');self.addCleanup(self.journal.conn.close)
        self.c=contract();self.events=[]

    def append(self,kind,payload,at,asset=None,source=None):
        self.events.append(self.journal.append(source_kind=kind,source_id='SYNTHETIC',stream_id='TEST',payload=payload,
            received_at=at,source_at=at if source is None else source,market=self.c['market'],asset_id=asset))

    def market_data(self,at,price=100):
        for channel in ('price.crypto','price.crypto.twap'):
            body={'symbol':'btcusd','source':'chainlink','timestamp':at,'value':price}
            if channel.endswith('twap'):body['window_seconds']=60
            self.append('OFFICIAL_TWAP' if channel.endswith('twap') else 'OFFICIAL_SPOT',{'channel':channel,'payload':body},at)
        for side in ('UP','DOWN'):
            self.append('ORDER_BOOK',{'event_type':'book','timestamp':str(at),'asset_id':'M1-'+side,
                'bids':[{'price':'.49','size':'100'}],'asks':[{'price':'.50','size':'100'}]},at,'M1-'+side)

    def run_replay(self,name,events=None,**kwargs):
        return run_strategy_scenarios(self.events if events is None else events,self.path/name,contracts=[self.c],clock_bound_ms=0,**kwargs)

    def test_cold_paper_replay_all_scenarios_independent_and_unfitted(self):
        self.market_data(240000);self.market_data(240500)
        report=self.run_replay('cold')
        for scenario,item in report['scenarios'].items():
            self.assertEqual(item['replay_kind'],'FULL_STRATEGY_CAUSAL')
            self.assertEqual(item['wallets'][0]['cash_confirmed'],'100')
            self.assertEqual(item['intents'],[]);self.assertGreater(item['counts']['features'],0)
            self.assertIn('MODEL_UNFITTED',item['reason_codes'])
            self.assertEqual(item['economic_space']['scenario_id'],scenario)
            self.assertTrue(item['config_sha256']);self.assertTrue(item['code_manifest_sha256'])
            self.assertTrue(item['ledger_event_sha256']);self.assertEqual(item['fixed_seed'],20261106)
        self.assertEqual(len({x['economic_space']['portfolio_run_id'] for x in report['scenarios'].values()}),3)

    def test_clock_unknown_is_not_silently_assumed_zero(self):
        self.market_data(240000)
        report=run_strategy_scenarios(self.events,self.path/'clock',contracts=[self.c])
        for item in report['scenarios'].values():self.assertIn('CLOCK_UNVERIFIED',item['reason_codes'])

    def test_future_contract_cannot_supply_past_baseline(self):
        self.c['captured_at']=250000
        from polybot2other.hengzhun.contracts import contract_hash
        self.c['contract_sha256']=contract_hash(self.c);self.market_data(240000)
        report=self.run_replay('future-contract')
        for item in report['scenarios'].values():self.assertEqual(item['counts']['features'],0)

    def test_replay_rejects_input_reorder_and_reused_output(self):
        self.market_data(240000);self.market_data(240500)
        with self.assertRaisesRegex(ValueError,'REPLAY_ORDER_UNDETERMINED'):self.run_replay('bad',list(reversed(self.events)))
        self.run_replay('once')
        with self.assertRaises(FileExistsError):self.run_replay('once')

    def test_multiweek_empty_tail_uses_registered_timers_not_250ms_polling(self):
        self.market_data(240000)
        calls=[];original=HengzhunRuntime.tick
        def tracked(runtime,now):calls.append(now);return original(runtime,now)
        with patch.object(HengzhunRuntime,'tick',tracked):
            report=self.run_replay('sparse',final_at=7*86400000,trade_start_at=0,evaluation_end_at=7*86400000)
        self.assertLess(len(calls),1000)
        for value in report['scenarios'].values():
            self.assertEqual(len(value['daily_nav']),7)
            self.assertTrue(all(x['nav']=='100' and x['complete'] for x in value['daily_nav']))

    def test_full_strategy_future_tails_do_not_rewrite_past_actions_or_ledger(self):
        # The numerical/model layer has separate golden/fit tests. Here explicit
        # synthetic predictive doubles isolate the complete causal orchestration.
        for at in range(230000,246001,500):self.market_data(at)
        prefix=deepcopy(self.events)
        self.market_data(247000,80);down=deepcopy(self.events)
        self.events=deepcopy(prefix);self.market_data(247000,120);up=deepcopy(self.events)
        self.events=deepcopy(prefix);self.append('OTHER',{'fault_type':'DISCONNECT'},247000);disconnected=deepcopy(self.events)
        original=HengzhunRuntime._contract
        def synthetic_contract(runtime,market,now):
            value=original(runtime,market,now);runtime.model={'artifact_status':'SYNTHETIC_CAUSALITY_TEST_ONLY'}
            runtime._last_model_market=value['market']['market_uid'];return value
        def prior(runtime,c,now):
            p=.8 if float(runtime.reference.get('spot',{}).get('value',100))>=100 else .2
            return {'spot':runtime.reference.get('spot',{}).get('value','100'),'baseline':'100','known_integral_usd_seconds':'0','known_seconds':0,
                'mean_unadjusted_usd':'100','mean_adjusted_usd':'100','variance_rate_usd2_per_second':'1',
                'process_variance_usd2':'20','total_variance_usd2':'21','proxy_bias_bps':0.,'proxy_sd_bps':1.,
                'proxy_sample_count':120,'proxy_p95_abs_error_bps':1.,'proxy_status':'READY','fast_return_count':24,
                'slow_return_count':120,'volatility_span_seconds':600,'p_raw_up':p,'p_raw_down':1-p},SimpleNamespace(calibrated=True)
        def fitted(runtime,c,books,prior,now):
            p=prior['p_raw_up']
            return {'model_artifact_sha256':digest(runtime.model),'model_version':'SYNTHETIC_CAUSALITY_TEST_ONLY',
                'feature_order':['u','v','u_times_h'],'features':[p,0.,0.],'standardized_features':[p,0.,0.],
                'p_fit_up':p,'p10_up':p,'p90_up':p,'guard_up':max(0,p-.02),'guard_down':max(0,1-p-.02),
                'bootstrap_success_count':100,'inference_success_count':100,'model_available_at':0,'model_expires_at':1000000}
        with patch.object(HengzhunRuntime,'_contract',synthetic_contract),patch.object(HengzhunRuntime,'_prior',prior),patch.object(HengzhunRuntime,'_fitted',fitted):
            reports=[self.run_replay('tail-'+str(i),events) for i,events in enumerate((down,up,disconnected,down))]
        for scenario in ('S1','S2','S3'):
            self.assertEqual(reports[0]['scenarios'][scenario]['economic_result_sha256'],reports[3]['scenarios'][scenario]['economic_result_sha256'])
            first=[]
            for i,report in enumerate(reports):
                item=report['scenarios'][scenario]
                self.assertEqual(len(item['intents']),1);self.assertEqual(item['intents'][0]['side'],'UP')
                with sqlite3.connect(item['feature_database']) as con:
                    frames=[json.loads(x[0]) for x in con.execute('SELECT json FROM features WHERE at<=246000 ORDER BY at')]
                decisions=[]
                for f in frames:
                    for book in (f['up_book'],f['down_book']):
                        if book:self.assertLessEqual(max(book['source_at'],book['received_at']),f['decision_at'])
                    if f['t_obs'] is not None:self.assertLessEqual(f['t_obs'],f['decision_at'])
                    decisions.append({k:f[k] for k in ('decision_at','prior','fitted','candidates','decision','decision_reasons')})
                with sqlite3.connect(item['ledger_database']) as con:
                    events=[json.loads(x[0]) for x in con.execute('SELECT payload FROM hz_ledger_events WHERE at_ms<=246000 ORDER BY rowid')]
                # Namespace/provenance IDs differ for independent experiment files;
                # compare deterministic economic effects and their event clocks.
                economics=[{k:e.get(k) for k in ('kind','effective_at','deltas','reason_codes','wallet_before','wallet_after')} for e in events]
                actions=[{k:o.get(k) for k in ('side','limit_price','quantity_shares','submitted_at','send_attempts')} for o in item['intents']]
                state=(digest(decisions),digest(economics),actions)
                if not first:first=state
                else:self.assertEqual(state,first)

    def test_b2_baseline_uses_registered_uncalibrated_policy_in_separate_wallet(self):
        from polybot2other.hengzhun.research_pipeline import baseline_factory
        from test_hengzhun_runtime import synthetic_prediction
        for at in range(230000,246001,500):self.market_data(at)
        def prior(runtime,c,now):return synthetic_prediction(runtime)[0],SimpleNamespace(calibrated=True)
        with patch.object(HengzhunRuntime,'_prior',prior):
            report=self.run_replay('baseline-b2',baseline_policy_factory=baseline_factory('B2_PRIOR',registered_at=230000))
        for item in report['scenarios'].values():
            self.assertEqual(len(item['intents']),1)
            order=item['intents'][0];self.assertEqual(order['record_type'],'BaselineOrderIntent')
            self.assertEqual(order['calibration_status'],'UNCALIBRATED');self.assertIsNone(order['model_artifact_sha256'])
            self.assertEqual(item['strategy_id'],'B2_PRIOR');self.assertIsNone(item['signal_shadow'])
            self.assertEqual(item['unexplained_reconciliation_differences'],0)

    def test_exported_derived_provenance_is_not_reingested_as_market_data(self):
        self.market_data(240000)
        manifest=self.journal.provenance(240000);self.events.append(manifest)
        report=self.run_replay('derived')
        for value in report['scenarios'].values():
            self.assertEqual(value['derived_provenance_events_skipped'],1)
            self.assertEqual(value['events_replayed'],4)
            with sqlite3.connect(value['feature_database']) as con:
                records=[json.loads(row[0]) for row in con.execute('SELECT json FROM raw_events')]
            copies=[row for row in records if row['source_id']=='gamma-read-only' and row['payload']==manifest['payload']]
            self.assertEqual(copies,[])
