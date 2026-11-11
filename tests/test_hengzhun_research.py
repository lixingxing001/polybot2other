"""合成研究夹具验证算法和防泄漏合同；不是历史收益证明。"""
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
import tempfile
import unittest

from polybot2other.hengzhun.config import load_config
from polybot2other.hengzhun.research import (
    DAY_MS, TimeSplit, walk_forward_windows, partition_rows, probability_metrics,
    paired_daily_bootstrap, bootstrap_sensitivity, circular_block_indices,
    summarize_channels, concentration_report, assess_research_gates, ExperimentRegistry,
    probability_metric_intervals,
)


def row(key, opened, offset=210000, **overrides):
    return {"condition_id": key, "rule_version": "twap60-v1", "window_open_at": opened,
            "decision_at": opened+offset, "label_available_at": opened+400000,
            "y": 1, "p_fit_up": .7, "p_market_up": .6, **overrides}


def days(values, start=date(2026,1,1)):
    return [{"day":(start+timedelta(days=i)).isoformat(), "net_return_usd":str(v)} for i,v in enumerate(values)]


class ResearchSplitsTests(unittest.TestCase):
    def setUp(self):
        self.start=20000*DAY_MS
        self.split=walk_forward_windows(self.start,self.start+42*DAY_MS)[0]

    def test_full_windows_only_and_test_weeks_never_overlap(self):
        self.assertEqual(walk_forward_windows(self.start,self.start+41*DAY_MS),[])
        windows=walk_forward_windows(self.start,self.start+77*DAY_MS)
        self.assertEqual(len(windows),6)
        for left,right in zip(windows,windows[1:]):
            self.assertEqual(left.test_end_at,right.test_start_at)
            self.assertEqual(right.training_start_at-left.training_start_at,7*DAY_MS)
        with self.assertRaisesRegex(ValueError,'FULL_UTC'):walk_forward_windows(self.start+1,self.start+77*DAY_MS)

    def test_ex007_future_label_is_excluded_even_if_market_already_closed(self):
        opened=self.split.training_end_at-300000
        future=row('future',opened,label_available_at=self.split.training_end_at+30000)
        past=row('past',opened-300000,label_available_at=self.split.training_end_at)
        result=partition_rows([future,past],self.split)
        self.assertEqual([x['condition_id'] for x in result['train']],['past'])
        self.assertEqual(result['excluded'][0]['reason'],'LABEL_NOT_YET_AVAILABLE')

    def test_group_weights_and_no_direction_or_landmark_split(self):
        records=[row('A',self.start,offset=offset) for offset in (210000,225000,240000,255000,270000)]
        records.append(row('B',self.split.validation_start_at))
        result=partition_rows(records,self.split)
        self.assertEqual(len(result['train']),5)
        self.assertAlmostEqual(sum(x['market_weight'] for x in result['train']),1)
        self.assertEqual({x['condition_id'] for x in result['validation']},{'B'})
        self.assertEqual(result['manifest_hash'],partition_rows(list(reversed(records)),self.split)['manifest_hash'])

    def test_source_opening_time_conflict_is_not_fixed_by_using_first_row(self):
        with self.assertRaisesRegex(ValueError,'OPEN_TIME_CONFLICT'):
            partition_rows([row('A',self.start),row('A',self.start+300000)],self.split)

    def test_unverified_label_and_nonbinary_results_never_enter_classifier(self):
        records=[row('A',self.start,label_verified_at=self.split.training_end_at+1),
                 row('B',self.start,y=None),row('C',self.start,y=True)]
        result=partition_rows(records,self.split)
        self.assertEqual(result['train'],[])
        self.assertEqual(len(result['excluded']),3)

    def test_late_publication_cannot_retroactively_trade_early_test_period(self):
        opened=self.split.test_start_at
        late=opened+DAY_MS
        records=[row('early',opened),row('after',late)]
        result=partition_rows(records,self.split,publication_at=late)
        self.assertEqual([x['condition_id'] for x in result['test']],['after'])
        self.assertEqual(result['excluded'][0]['reason'],'MODEL_NOT_YET_PUBLISHED')

    def test_model_publication_cannot_precede_validation_end(self):
        with self.assertRaisesRegex(ValueError,'PUBLICATION'):
            partition_rows([],self.split,publication_at=self.split.validation_end_at-1)


class ResearchStatisticsTests(unittest.TestCase):
    def test_market_not_tick_weighted_metrics_and_common_comparison_rows(self):
        records=[row('many',0,offset=i,p_fit_up=.9,p_market_up=.6) for i in range(5)]
        records.append(row('one',300000,p_fit_up=.1,p_market_up=.6))
        result=probability_metrics(records)
        self.assertAlmostEqual(result['model']['brier'],(.01+.81)/2)
        self.assertAlmostEqual(result['market']['brier'],.16)
        self.assertEqual(result['independent_markets'],2)
        self.assertEqual(result['landmark_rows'],6)
        records[0]['p_market_up']=None
        with self.assertRaisesRegex(ValueError,'INVALID_PREDICTION'):probability_metrics(records)

    def test_probability_zero_one_are_finite_for_log_loss_without_faking_brier(self):
        metrics=probability_metrics([row('A',0,p_fit_up=0,p_market_up=1)])
        self.assertEqual(metrics['model']['brier'],1)
        self.assertGreater(metrics['model']['log_loss'],20)

    def test_paired_circular_bootstrap_is_deterministic_and_uses_same_indices(self):
        sample=days([1,2,-1,4,2,0,3])
        first=paired_daily_bootstrap(sample,sample)
        second=paired_daily_bootstrap(sample,sample)
        self.assertEqual(first,second)
        self.assertEqual(first['improvement']['point_estimate'],0)
        self.assertEqual(first['improvement']['one_sided_lower'],0)
        self.assertEqual(first['improvement']['test_status'],'FAIL')
        self.assertTrue(first['not_an_account_replay']);self.assertIsNone(first['drawdown_from_resampling'])

    def test_circular_wrap_and_final_block_truncation(self):
        class Fixed:
            def randrange(self,n):return n-1
        self.assertEqual(circular_block_indices(5,3,Fixed()),[4,0,1,4,0])

    def test_two_tests_and_multiple_candidates_adjust_alpha(self):
        result=paired_daily_bootstrap(days([1]*7),days([0]*7),candidate_count=4)
        self.assertEqual(result['one_sided_alpha'],.05/(2*4))
        self.assertEqual(result['net']['test_status'],'PASS')
        self.assertEqual(result['improvement']['test_status'],'PASS')
        with self.assertRaisesRegex(ValueError,'BELOW_SPEC'):
            paired_daily_bootstrap(days([1]),repetitions=19999)

    def test_missing_day_or_valuation_is_not_deleted(self):
        records=days([1,2,3]);records.pop(1)
        report=paired_daily_bootstrap(records)
        self.assertEqual(report['status'],'INSUFFICIENT')
        self.assertEqual(report['missing_days'],['2026-01-02'])
        records=days([1,2]);records[1]['net_return_usd']=None
        self.assertEqual(paired_daily_bootstrap(records)['reasons'],['DAILY_VALUATION_MISSING'])

    def test_unknown_valuation_uses_only_explicit_conservative_bounds(self):
        records=days([1,2]);records[1].update(net_return_usd=None,lower_return_usd='-10',upper_return_usd='10')
        baseline=days([0,0]);baseline[1].update(net_return_usd=None,lower_return_usd='-3',upper_return_usd='3')
        result=paired_daily_bootstrap(records,baseline,conservative_bounds=True)
        self.assertTrue(result['conservative_bounds_used'])
        self.assertEqual(result['net']['point_estimate'],-4.5)
        self.assertEqual(result['improvement']['point_estimate'],-6)

    def test_unknown_search_history_stays_exploratory_despite_positive_result(self):
        result=paired_daily_bootstrap(days([100]*7),candidate_history_known=False)
        self.assertEqual(result['status'],'EXPLORATORY')
        self.assertEqual(result['net']['test_status'],'EXPLORATORY')

    def test_all_registered_block_lengths_are_reported(self):
        reports=bootstrap_sensitivity(days([1]*7))
        self.assertEqual(set(reports),{'1','3','7'})
        self.assertTrue(all(r['seed']==20261106 and r['repetitions']==20000 for r in reports.values()))

    def test_probability_intervals_are_paired_and_keep_market_weights(self):
        records=[]
        for i in range(7):
            records.extend(row(str(i),i*DAY_MS,offset=210000+j,p_fit_up=.6+i*.01,p_market_up=.6+i*.01)
                           for j in range(i+1))
        result=probability_metric_intervals(records)
        self.assertEqual(result['status'],'COMPUTED')
        self.assertEqual(result['point_metrics']['independent_markets'],7)
        self.assertEqual(result['interval_95']['delta_brier'],[0.,0.])
        self.assertEqual(result['interval_95']['delta_log_loss'],[0.,0.])
        self.assertEqual(result['method_scope'],'DECLARED_DIAGNOSTIC_NOT_G2_THRESHOLD')


class ResearchRegistryTests(unittest.TestCase):
    def test_ex008_parameter_change_needs_new_experiment_old_report_is_immutable(self):
        with tempfile.TemporaryDirectory() as directory:
            registry=ExperimentRegistry(Path(directory)/'registry.sqlite3')
            self.addCleanup(registry.close)
            config=load_config()
            first=registry.register('H1',config,{'events':['source_hash']},strategy_hash='a'*64)
            report={**first,'status':'FAIL'}
            digest=registry.save_report('report1','H1',report)
            self.assertEqual(registry.save_report('report1','H1',report),digest)
            modified=deepcopy(config);modified['entry']['minimum_guard_edge']='0.04'
            with self.assertRaisesRegex(ValueError,'NEW_RUN'):
                registry.register('H1',modified,{'events':['source_hash']},strategy_hash='a'*64)
            second=registry.register('H2',modified,{'events':['source_hash']},strategy_hash='a'*64,parent_run_id='H1')
            self.assertNotEqual(first['config_hash'],second['config_hash'])
            with self.assertRaisesRegex(ValueError,'FROZEN_INPUT'):
                registry.save_report('report2','H1',{**second,'status':'PASS'})
            with self.assertRaisesRegex(ValueError,'IMMUTABLE'):
                registry.save_report('report1','H1',{**report,'status':'PASS'})

    def test_ex046_shadow_cannot_be_added_to_portfolio_profit_and_tail_stays_visible(self):
        base={'mode':'REPLAY','portfolio_run_id':'run1','scenario_id':'S1','rule_version':'rule1'}
        outcomes=[{**base,'ledger_scope':'SIGNAL_SHADOW','condition_id':'s1','economic_status':'CLOSED_CLAIMED','realized_pnl_usd':'1000'},
                  {**base,'ledger_scope':'PAPER_PORTFOLIO','condition_id':'p1','economic_status':'CLOSED_CLAIMED','realized_pnl_usd':'-5'},
                  {**base,'ledger_scope':'PAPER_PORTFOLIO','condition_id':'p2','economic_status':'UNKNOWN'}]
        report=summarize_channels(outcomes)
        self.assertIsNone(report['combined_pnl_usd'])
        portfolio=next(x for x in report['economic_spaces'] if x['ledger_scope']=='PAPER_PORTFOLIO')
        shadow=next(x for x in report['economic_spaces'] if x['ledger_scope']=='SIGNAL_SHADOW')
        self.assertEqual(portfolio['realized_pnl_usd'],'-5')
        self.assertEqual(portfolio['uncertain_markets'],1)
        self.assertEqual(portfolio['result_status'],'PENDING_TAIL')
        self.assertEqual(shadow['hypothetical_pnl_usd'],'1000')
        self.assertNotIn('realized_pnl_usd',shadow)

    def test_concentration_preserves_losing_and_nontrading_days(self):
        report=concentration_report(['10','-8','0','5'],['10','5','-8'])
        self.assertAlmostEqual(report['largest_day_share_of_positive_returns'],2/3)
        self.assertEqual(report['day_count'],4)
        self.assertEqual(report['nontrading_or_zero_days'],1)


class ResearchGatesTests(unittest.TestCase):
    def evidence(self):
        statistics=bootstrap_sensitivity(days([1]*42),days([0]*42))
        return {'G0':{'executed':True,'evidence_verified':True,'failed':0},
                'G1':{'executed':True,'evidence_verified':True,'failed':0},
                'model':{'artifact_verified':True,'manifests_verified':True,'coverage_reported':True,
                         'probability_intervals_reported':True,'train_markets':3000,'complete_train_days':20,
                         'up_train_markets':1500,'down_train_markets':1500,'validation_markets':1000,
                         'bootstrap_success':90,'train_window_days':28,'validation_window_days':7,
                         'validation_metrics':{'model':{'brier':.2,'log_loss':.5},'market':{'brier':.21,'log_loss':.51}}},
                'study':{'manifests_verified':True,'continuous_accounts_verified':True,'complete_daily_sequence':True,
                         'tails_accounted':True,'concentration_reported':True,'nonoverlapping_test_weeks':6,
                         'shadow_filled_markets':1000,'portfolio_filled_markets':100,'unexplained_reconciliation_differences':0,
                         'maximum_drawdown':.1,'bootstrap':statistics,'one_extra_tick_cost_pnl_usd':'2',
                         'scenarios':{name:{'full_replay':True,'manifest_verified':True,'net_pnl_usd':'3'} for name in ('S1','S2','S3')}},
                'baseline':{'frozen_source_verified':True,'unified_risk_supports_improvement':True}}

    def test_no_evidence_never_passes_any_research_gate(self):
        report=assess_research_gates({})
        self.assertTrue(all(report[key]['status']=='INSUFFICIENT' for key in ('G0','G1','G2','G3','G4')))
        self.assertEqual(report['execution_realism']['status'],'ASSUMED')

    def test_synthetic_complete_evidence_and_realism_still_not_claimed(self):
        report=assess_research_gates(self.evidence())
        self.assertTrue(all(report[key]['status']=='PASS' for key in ('G0','G1','G2','G3','G4')))
        self.assertEqual(report['execution_realism']['status'],'ASSUMED')
        self.assertFalse(report['execution_realism']['live_execution_enabled'])

    def test_ex050_stress_loss_is_execution_fragile_and_missing_scenario_insufficient(self):
        evidence=self.evidence();evidence['study']['scenarios']['S3']['net_pnl_usd']='-1'
        self.assertEqual(assess_research_gates(evidence)['G3']['status'],'EXECUTION_FRAGILE')
        del evidence['study']['scenarios']['S2']
        self.assertEqual(assess_research_gates(evidence)['G3']['status'],'INSUFFICIENT')

    def test_verification_minimums_and_probability_metrics_each_apply(self):
        evidence=self.evidence();evidence['model']['complete_train_days']=19
        self.assertEqual(assess_research_gates(evidence)['G2']['status'],'INSUFFICIENT')
        evidence['model']['complete_train_days']=20;evidence['model']['validation_metrics']['model']['brier']=.3
        self.assertEqual(assess_research_gates(evidence)['G2']['status'],'FAIL')

    def test_fake_pass_flag_does_not_override_nonpositive_statistical_bound(self):
        evidence=self.evidence();evidence['study']['bootstrap']['3']['net']['one_sided_lower']=-1
        self.assertEqual(assess_research_gates(evidence)['G3']['status'],'FAIL')
        evidence['study']['bootstrap']['3']['net']['one_sided_lower']=1
        evidence['study']['bootstrap']['3']['repetitions']=50
        self.assertEqual(assess_research_gates(evidence)['G3']['status'],'INSUFFICIENT')


if __name__=='__main__':unittest.main()
