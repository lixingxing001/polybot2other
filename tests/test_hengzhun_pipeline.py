"""Multi-week orchestration contract with synthetic empty-market observation clocks."""
from datetime import datetime,timezone
import json
from pathlib import Path
import tempfile
import unittest
from copy import deepcopy
from unittest.mock import patch

from polybot2other.hengzhun.data import EventJournal
from polybot2other.hengzhun.research import DAY_MS
from polybot2other.hengzhun.research_pipeline import run_research,_daily_changes,baseline_factory,probability_support_report,tail_risk_report,_verified_acceptance
from polybot2other.hengzhun.config import load_config,config_hash
from polybot2other.hengzhun.contracts import contract_hash


class HengzhunPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.start=20000*DAY_MS

    def request(self,days):
        journal=EventJournal(self.root/'capture.sqlite3')
        try:
            events=[journal.append(source_kind='OTHER',source_id='synthetic-calendar',stream_id='calendar',payload={'marker':i},
                source_at=at,received_at=at) for i,at in enumerate((self.start,self.start+days*DAY_MS))]
        finally:journal.conn.close()
        values={'events':events,'contracts':[],'landmark_rows':[],'coverage':{'complete_utc_days':[],'source':'SYNTHETIC_NO_MARKET_DATA'}}
        for name,value in values.items():(self.root/(name+'.json')).write_text(json.dumps(value))
        return {**{name:str(self.root/(name+'.json')) for name in values},'first_day_at':self.start,'end_at':self.start+days*DAY_MS,
            'candidate_count':1,'candidate_history_known':True,'rule_version':'synthetic-only','rule_contract_hash':'a'*64,
            'code_revision':'synthetic-test','clock_bound_ms':0}

    def test_insufficient_calendar_span_does_not_run_or_invent_folds(self):
        result=run_research(self.request(41),self.root/'short')
        self.assertEqual(result['status'],'INSUFFICIENT');self.assertEqual(result['folds'],[])
        self.assertFalse((self.root/'short/strategy').exists())
        self.assertTrue(all(result['gates'][g]['status']=='INSUFFICIENT' for g in ('G0','G1','G2','G3','G4')))

    def test_two_test_weeks_share_one_real_replay_wallet_per_scenario(self):
        result=run_research(self.request(49),self.root/'study')
        self.assertEqual(len(result['folds']),2);self.assertEqual(result['published_models'],0)
        self.assertTrue(all(f['training_status']=='INSUFFICIENT' for f in result['folds']))
        self.assertEqual(set(result['strategy']['scenarios']),{'S1','S2','S3'})
        for scenario in ('S1','S2','S3'):
            run=result['strategy']['scenarios'][scenario]
            self.assertEqual(run['replay_kind'],'FULL_STRATEGY_CAUSAL')
            self.assertEqual(len(run['wallets']),1)
            self.assertEqual(run['wallets'][0]['cash_confirmed'],'100')
            self.assertEqual(len(result['statistics'][scenario]['daily']),14)
            self.assertTrue(all(r['net_return_usd']=='0' for r in result['statistics'][scenario]['daily']))
            self.assertEqual(result['baselines']['B2']['scenarios'][scenario]['baseline_registration']['baseline_id'],'B2_PRIOR')
        self.assertEqual(result['baselines']['B1_original_account']['status'],'BASELINE_UNAVAILABLE')
        self.assertEqual(result['gates']['G2']['status'],'INSUFFICIENT')
        self.assertEqual(result['gates']['G3']['status'],'INSUFFICIENT')
        self.assertEqual(result['baselines']['B3']['metrics']['status'],'INSUFFICIENT')
        with self.assertRaises(FileExistsError):run_research(self.request(49),self.root/'study')

    def test_missing_nav_keeps_calendar_and_both_affected_returns_unknown(self):
        rows=[{'at':self.start+DAY_MS-1,'nav':'101'},
              {'at':self.start+3*DAY_MS-1,'nav':'103'},
              {'at':self.start+4*DAY_MS-1,'nav':'104'}]
        daily,dd=_daily_changes(rows,self.start,self.start+4*DAY_MS)
        self.assertEqual([r['net_return_usd'] for r in daily],['1',None,None,'1'])
        self.assertEqual(len(daily),4)

    def test_baseline_registration_changes_with_scenario_config(self):
        factory=baseline_factory('B2_PRIOR',registered_at=self.start)
        values=[]
        for scenario in ('S1','S2','S3'):
            cfg=load_config({'runtime':{'mode':'REPLAY'}},scenario=scenario)
            registration,_=factory(scenario,cfg,'a'*64,'run')
            self.assertEqual(registration['config_sha256'],config_hash(cfg));values.append(registration['config_sha256'])
        self.assertEqual(len(set(values)),3)

    def test_validated_candidate_publishes_after_measured_fit_at_next_market_boundary(self):
        # Reuse the real synthetic fitted artifact from the separately tested
        # calibration layer; the injected trainer isolates orchestration here.
        from test_hengzhun_model import CalibrationTests
        from test_hengzhun_pricing import verified_contract
        CalibrationTests.setUpClass();self.addCleanup(CalibrationTests.tearDownClass)
        self.start=CalibrationTests.start
        request=self.request(42);request['rule_version']='fixture-rule'
        opened=CalibrationTests.val_end+300000
        contract=verified_contract(opened=opened);contract['rules']['rule_version']='fixture-rule'
        contract['contract_sha256']=contract_hash(contract)
        Path(request['contracts']).write_text(json.dumps([contract]))
        with patch('polybot2other.hengzhun.research_pipeline.train_candidate',return_value=deepcopy(CalibrationTests.result)) as fit:
            result=run_research(request,self.root/'fitted-study')
        self.assertEqual(fit.call_count,1);self.assertEqual(result['published_models'],1)
        release=json.loads(Path(result['folds'][0]['release']['uri']).read_text())
        self.assertEqual(release['release']['available_at'],opened)
        self.assertGreaterEqual(opened,CalibrationTests.val_end+result['folds'][0]['measured_offline_fit_duration_ms'])
        self.assertEqual(release['release']['publication_boundary_market_uid'],contract['market']['market_uid'])
        self.assertEqual(len(result['strategy']['scenarios']['S1']['daily_nav']),7)
        self.assertEqual(result['strategy']['scenarios']['S1']['intents'],[])

    def test_probability_support_and_tail_diagnostics_keep_market_weights_and_missing_days(self):
        rows=[{'condition_id':'A','rule_version':'test','y':1,'p_fit_up':.6,'p_market_up':.5,
            'tau_wall_seconds':60,'best_ask_up':'.59','volatility_bps_sqrt_second':.3} for _ in range(5)]
        report=probability_support_report(rows)
        self.assertEqual(len(report['groups']),4)
        self.assertTrue(all(g['independent_markets']==1 and g['landmark_rows']==5 for g in report['groups']))
        risk=tail_risk_report([{'net_return_usd':str(v)} for v in (-2,-3,0,4,-1)],['-5','3'])
        self.assertEqual(risk['longest_consecutive_losing_days'],2)
        self.assertEqual(risk['worst_day_usd'],-3)
        self.assertEqual(tail_risk_report([{'net_return_usd':None}],[])['status'],'INSUFFICIENT')
        forged=self.root/'fake-pass.json'
        forged.write_text(json.dumps({'G1':'PASS','case_count':50,'cases':[{'acceptance_status':'PASS','coverage_reviewed':True}]*50}))
        self.assertFalse(_verified_acceptance(forged))


if __name__=='__main__':unittest.main()
