"""供应方 27 数值样例及因果模型合同；训练样本均为临时人工夹具。"""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime,timezone
from decimal import Decimal
import json
import math
from pathlib import Path
import tempfile
import unittest

import yaml
from polybot2other.hengzhun import model as m
from polybot2other.hengzhun import calibration as c
from polybot2other.hengzhun.config import load_config,config_hash,SCENARIOS
from polybot2other.hengzhun.contracts import SPEC_DIR,validate_record

ACCEPTANCE_CASES = {"EX-004":"test_ex004_unfitted_never_predicts", "EX-005":"test_ex005_domain_and_invalid_bootstrap_reject",
                    "EX-007":"test_ex007_future_labels_rejected", "EX-008":"test_ex008_training_never_uses_validation_in_fit"}


class GoldenAndModelTests(unittest.TestCase):
    def assert_expected(self, actual, expected, tolerance=1e-9):
        if isinstance(expected,dict):
            for key,value in expected.items():self.assert_expected(actual[key],value,tolerance)
        elif isinstance(expected,bool):self.assertIs(actual,expected)
        elif isinstance(actual,Decimal) and isinstance(expected,str):self.assertEqual(actual,Decimal(expected))
        elif isinstance(expected,(int,float)):self.assertTrue(math.isclose(float(actual),float(expected),rel_tol=1e-12,abs_tol=tolerance),(actual,expected))
        else:self.assertEqual(actual,expected)

    def test_all_27_supplied_golden_cases_against_implementation(self):
        cases=json.loads((SPEC_DIR/'golden_cases.json').read_text())['cases'];self.assertEqual(len(cases),27)
        operations={name:getattr(m,name) for name in ('twap_prior','forecast_from_timestamps','source_timing','previous_tick_integral',
                    'effective_cost_cash','conservative_guards','cost_cap_from_guard','maximum_price_for_cash_cost','floor_to_tick',
                    'value_gates','budget_to_shares','equal_budget_pair_math','settlement_label','reference_trade_example')}
        operations['realized_variance']=m.realized_variance_from_log_returns
        for case in cases:
            with self.subTest(case=case['id']):
                if case.get('expected_error'):
                    with self.assertRaises(ValueError):operations[case['operation']](**case['inputs'])
                else:self.assert_expected(operations[case['operation']](**case['inputs']),case['expected'],case.get('abs_tol',1e-9))

    def test_94_defaults_match_supplied_yaml_and_schema(self):
        expected=yaml.safe_load((SPEC_DIR/'strategy_config.yaml').read_text())
        actual=load_config();self.assertEqual(actual,expected)
        self.assertEqual(sum(len(v) for v in actual.values())-1,94)
        validate_record(actual,'config.schema.json')
        with self.assertRaises(ValueError):load_config({'runtime':{'mode':'LIVE'}})
        with self.assertRaises(ValueError):load_config({'runtime':{'live_execution_enabled':True}})
        with self.assertRaises(ValueError):load_config({'entry':{'unknown':1}})
        self.assertNotEqual(config_hash(actual),config_hash(load_config(scenario='S2')))
        self.assertEqual(load_config(scenario='S3')['paper']['total_delay_ms'],5000)

    def test_delayed_price_is_not_advanced_to_wall_clock(self):
        prediction=m.predict_terminal_twap(99,100,90000,58000,60000,4,known_integral=28*103,
                                           proxy=m.ProxyEstimate(0,1,0,None,False,'PROXY_UNCALIBRATED'))
        self.assertEqual(prediction.tau_wall_seconds,30);self.assertEqual(prediction.tau_model_seconds,32)
        self.assertAlmostEqual(prediction.mean,(28*103+32*99)/60)
        with self.assertRaises(ValueError):m.predict_terminal_twap(99,100,90000,61000,60000,4,known_integral=0)

    def test_locf_visibility_conflicts_and_max_gap(self):
        points=[m.PriceObservation(i,i,100+i/1000) for i in range(0,5001,1000)]
        before=m.integrate_spot(points,0,5000,5000)
        future=m.PriceObservation(2500,6000,200)
        self.assertEqual(before,m.integrate_spot([*points,future],0,5000,5000))
        with self.assertRaisesRegex(ValueError,'DATA_CONFLICT'):m.visible_points([*points,m.PriceObservation(1000,1001,999)],5000)
        with self.assertRaisesRegex(ValueError,'INTEGRAL_GAP'):m.integrate_spot([points[0],points[-1]],0,5000,5000)

    def test_frozen_utc_grid_rejects_late_ticks_and_does_not_bridge_holes(self):
        points=[m.PriceObservation(i,i,100+i/1e6) for i in range(0,610001,1000)]
        first=m.causal_grid(points,0,610000)
        late=m.PriceObservation(4999,6001,101)
        self.assertEqual(first[1],m.causal_grid([*points,late],0,610000)[1])
        sparse=list(first);sparse[-3]=m.GridPoint(sparse[-3].grid_at_ms,None)
        result=m.estimate_volatility(sparse,610000,100)
        self.assertEqual(result.fast_count,22);self.assertEqual(result.slow_count,118)

    def test_volatility_floor_and_shock_use_pre_shock_variance(self):
        grid=[m.GridPoint(i*5000,100.) for i in range(121)]
        floor=m.estimate_volatility(grid,600000,100)
        self.assertAlmostEqual(floor.sigma_squared,(100*.25/10000)**2)
        shock=[*grid[:-1],m.GridPoint(600000,101.)]
        estimate=m.estimate_volatility(shock,600000,101)
        self.assertEqual(estimate.shock_cooldown_until_ms,660000)
        self.assertGreater(estimate.sigma_squared,floor.sigma_squared)
        with self.assertRaisesRegex(ValueError,'VOL_WARMUP'):m.estimate_volatility(grid[1:],600000,100)
        missing_start=[m.GridPoint(0,None),*grid[1:]]
        with self.assertRaisesRegex(ValueError,'VOL_WARMUP'):m.estimate_volatility(missing_start,600000,100)

    def test_proxy_spacing_median_rms_p95_and_cold_shadow(self):
        records=[m.ProxyErrorObservation(i*60000,i*60000,2.) for i in range(120)]
        result=m.proxy_error_stats(records,120*60000)
        self.assertEqual(result.bias_bps,2);self.assertEqual(result.sd_bps,1);self.assertTrue(result.calibrated)
        cold=m.proxy_error_stats(records[:-1],120*60000);self.assertEqual(cold.bias_bps,0);self.assertFalse(cold.calibrated)
        self.assertEqual(m.proxy_error_stats([m.ProxyErrorObservation(i*60000,i*60000,4.) for i in range(120)],120*60000).reason_code,'PROXY_ERROR_EXCEEDED')
        with self.assertRaisesRegex(ValueError,'PROXY_SAMPLE_SPACING'):m.proxy_error_stats([*records,m.ProxyErrorObservation(119*60000+1,120*60000,0)],120*60000)


class CalibrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary=tempfile.TemporaryDirectory();cls.directory=Path(cls.temporary.name)
        cls.start=1767225600000;cls.train_end=cls.start+28*c.DAY_MS;cls.val_end=cls.train_end+7*c.DAY_MS
        cls.days=[datetime.fromtimestamp((cls.start+d*c.DAY_MS)/1000,timezone.utc).date().isoformat() for d in range(20)]
        def rows(start,count,per_day,prefix):
            result=[]
            for i in range(count):
                opened=start+(i//per_day)*c.DAY_MS+(i%per_day)*300000
                result.append({'condition_id':prefix+str(i),'rule_version':'fixture-rule','window_open_at':opened,
                    'decision_at':opened+210000,'label_available_at':opened+310000,'y':i%2,
                    'p_raw_up':.5,'p_market_up':.5,'tau_wall_seconds':90.})
            return result
        cls.training=rows(cls.start,3000,150,'train-');cls.validation=rows(cls.train_end,1000,150,'validation-')
        cls.kwargs={'complete_utc_days':cls.days,'training_window':{'start_at':cls.start,'end_at':cls.train_end},
            'validation_window':{'start_at':cls.train_end,'end_at':cls.val_end},'evaluated_at':cls.val_end,
            'rule_version':'fixture-rule','rule_contract_hash':'a'*64,'output_dir':cls.directory,
            'code_revision':'synthetic-unit-test-only','coverage_evidence':{'source':'SYNTHETIC_UNIT_TEST_ONLY'}}
        cls.result=c.train_candidate(cls.training,cls.validation,**cls.kwargs)
        cls.artifact=cls.result['artifact']

    @classmethod
    def tearDownClass(cls):cls.temporary.cleanup()

    def test_logistic_weighted_transform_and_deterministic_fit(self):
        x=[(-2,1,0),(-1,1,0),(1,1,0),(2,1,0)];y=[0,0,1,1]
        fit=c.fit_logistic(x,y,[1,1,1,1])
        self.assertEqual(fit,c.fit_logistic(x,y,[1,1,1,1]))
        self.assertTrue(fit['transform']['degenerate_features'][1])
        self.assertGreater(c.predict_fit(fit,(1,1,0)),.5)
        self.assertLess(c.predict_fit(fit,(-1,1,0)),.5)
        with self.assertRaisesRegex(ValueError,'SINGLE_CLASS'):c.fit_logistic(x,[0]*4)

    def test_bootstrap_is_real_day_resampling_and_preserves_multiplicity(self):
        self.assertEqual(self.result['status'],'FITTED_CANDIDATE')
        validate_record(self.artifact,'model_artifact.schema.json')
        b=self.artifact['bootstrap'];self.assertEqual(b['successful_count'],100)
        draws=[json.loads(Path(item['draw_manifest']['uri']).read_text())['days'] for item in b['successful_models']]
        self.assertTrue(all(len(draw)==20 for draw in draws))
        self.assertTrue(any(len(set(draw))<20 for draw in draws))
        self.assertTrue(all(item['sample_counts']['total_market_weight_with_multiplicity']==3000 for item in b['successful_models']))
        c.validate_artifact(self.artifact)

    def test_ex004_unfitted_never_predicts(self):
        artifact=c.unfitted_artifact(self.val_end);validate_record(artifact,'model_artifact.schema.json')
        with self.assertRaisesRegex(ValueError,'MODEL_UNFITTED'):c.predict_calibrated(artifact,.6,.5,60,self.val_end,'fixture-rule','a'*64)
        result=c.train_candidate([],[],**{**self.kwargs,'complete_utc_days':[],'output_dir':self.directory/'empty'})
        self.assertEqual(result['status'],'INSUFFICIENT');self.assertNotIn('fit',result['artifact'])

    def test_ex005_domain_and_invalid_bootstrap_reject(self):
        published=c.publish_artifact(self.artifact,self.val_end,'fixture-boundary')
        result=c.predict_calibrated(published,.5,.5,60,self.val_end,'fixture-rule','a'*64)
        self.assertAlmostEqual(result['guard_up'],.48);self.assertAlmostEqual(result['guard_down'],.48)
        self.assertLess(result['guard_up']+result['guard_down'],1)
        with self.assertRaisesRegex(ValueError,'OUT_OF_DOMAIN'):c.predict_calibrated(published,.9999,.5,60,self.val_end,'fixture-rule','a'*64)
        invalid=deepcopy(published);invalid['bootstrap']['successful_models'][0]['fit']['parameters']['intercept']=float('nan')
        with self.assertRaises((ValueError,OverflowError)):c.predict_calibrated(invalid,.5,.5,60,self.val_end,'fixture-rule','a'*64)

    def test_expiry_rule_and_publication_boundary(self):
        with self.assertRaisesRegex(ValueError,'MODEL_PUBLICATION_BOUNDARY'):c.publish_artifact(self.artifact,self.val_end+1,'fixture-boundary')
        published=c.publish_artifact(self.artifact,self.val_end,'fixture-boundary')
        with self.assertRaisesRegex(ValueError,'MODEL_RULE_MISMATCH'):c.predict_calibrated(published,.5,.5,60,self.val_end,'another','a'*64)
        with self.assertRaisesRegex(ValueError,'MODEL_EXPIRED'):c.predict_calibrated(published,.5,.5,60,self.val_end+7*c.DAY_MS,'fixture-rule','a'*64)

    def test_ex007_future_labels_rejected(self):
        rows=deepcopy(self.training[:2]);rows[0]['label_available_at']=self.train_end+1
        with self.assertRaisesRegex(ValueError,'LABEL_LEAKAGE'):c.train_candidate(rows,self.validation,**self.kwargs)

    def test_ex008_training_never_uses_validation_in_fit(self):
        fit=self.artifact['fit'];x,y,w,_=c.dataset_arrays(self.training)
        self.assertEqual(fit,c.fit_logistic(x,y,w))
        wrong=[*self.validation];wrong[0]={**wrong[0],'condition_id':self.training[0]['condition_id']}
        with self.assertRaisesRegex(ValueError,'MARKET_SPLIT'):c.train_candidate(self.training,wrong,**self.kwargs)

    def test_source_hashes_are_verified_against_content_not_shape(self):
        file=Path(self.artifact['provenance']['training_manifest']['uri']);original=file.read_bytes()
        try:
            file.write_text('[]')
            with self.assertRaisesRegex(ValueError,'MODEL_PROVENANCE_INVALID'):c.validate_artifact(self.artifact)
        finally:file.write_bytes(original)


if __name__=='__main__':unittest.main()
