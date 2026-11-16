"""Causal learner and same-opportunity validation checks; no funded ledgers."""
from copy import deepcopy
import json
import unittest

from polybot2other.adaptive_v3 import (
    MODEL_VERSION, FEATURES, MAX_ADJUSTMENT, canonical_samples, feature_values,
    fit_model, predict, validate_candidate,
)

NOW = 2_000_000.


def training(index, **updates):
    row = {"round_id":f"market-{index}", "source_key":f"optimized:{index}",
        "provenance":"optimized_raw", "side":"Up", "base_score":.7,
        "entry_price":.6, "time_left_seconds":120., "opened_at":NOW-1000-index,
        "decision_at":NOW-1001-index, "settled_at":NOW-700-index,
        "won":1 if index % 10 < 7 else 0}
    row.update(updates)
    return row


def observation(index, **updates):
    decision = NOW-2*86400+index*1800
    row = {"round_id":f"observation-{index}", "decision_at":decision,
        "settled_at":decision+350, "ends_at":decision+300, "recorded_at":decision+1,
        "base_score":.7, "candidate_score":.76, "all_in_cost":.65, "min_edge":.02,
        "won":1, "model_version":MODEL_VERSION, "fee_source":"official-market",
        "label_source":"polymarket_official", "trained_before":decision-1, "training_fingerprint":"a"*64}
    row.update(updates)
    return row


class AdaptiveV3LearnerTests(unittest.TestCase):
    def test_sparse_history_has_zero_correction_without_suspending(self):
        model = fit_model([training(i) for i in range(10)], now=NOW)
        result = predict(model, training(200))
        self.assertEqual(model['state'], 'BASELINE_FALLBACK')
        self.assertEqual(result['score'], .7)
        self.assertEqual(result['delta'], 0.)
        self.assertFalse(result['contributions'])
        json.dumps(model, allow_nan=False)

    def test_fit_uses_only_prior_settlements_and_excludes_current_market(self):
        prior = [training(i) for i in range(100)]
        invalid = [training(500,settled_at=NOW),training(501,settled_at=NOW+1),
                   training(502,round_id='current'),training(503,provenance='legacy_adaptive'),
                   training(504,decision_at=NOW+2),training(505,exclusion_reason='nonterminal_exit')]
        model = fit_model(prior+invalid, now=NOW, round_id='current')
        clean = fit_model(prior, now=NOW, round_id='current')
        self.assertEqual(model['weights'], clean['weights'])
        self.assertEqual(model['training_fingerprint'],clean['training_fingerprint'])
        self.assertEqual(model['counts']['unique_markets'],100)
        self.assertLess(model['trained_before'],NOW)

    def test_future_label_changes_do_not_change_predictive_output(self):
        rows = [training(i) for i in range(100)]
        future = training(1000,settled_at=NOW+1)
        a = fit_model(rows+[future],now=NOW)
        b = fit_model(rows+[{**future,'won':1-future['won']}],now=NOW)
        self.assertEqual(predict(a,training(200)),predict(b,training(200)))

    def test_source_dedup_prefers_pretrade_observation_and_never_joins_labels(self):
        fill = training(1,won=0)
        shadow = training(2,round_id=fill['round_id'],won=1,provenance='shadow_v3_raw')
        rows,counts = canonical_samples([fill,shadow],now=NOW)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['won'],1)
        self.assertEqual(rows[0]['provenance'],'shadow_v3_raw')
        self.assertEqual(counts['exclusions']['duplicate_market'],1)

    def test_conditional_learning_can_increase_and_decrease_scores(self):
        rows = [training(i,side='Up' if i%2 else 'Down',won=1 if i%2 else 0)
                for i in range(160)]
        model = fit_model(rows,now=NOW)
        up = predict(model,training(201,side='Up'))
        down = predict(model,training(202,side='Down'))
        self.assertGreater(up['delta'],.01)
        self.assertLess(down['delta'],-.01)
        self.assertLessEqual(abs(up['delta']),MAX_ADJUSTMENT+1e-12)
        self.assertLessEqual(abs(down['delta']),MAX_ADJUSTMENT+1e-12)
        self.assertEqual(predict(model,training(202,side='Down')),down)

    def test_missing_features_are_explicit_and_sparse_features_are_disabled(self):
        rows=[training(i,**({'v3_momentum_10_bps':2.} if i<10 else {})) for i in range(100)]
        model=fit_model(rows,now=NOW)
        self.assertEqual(model['weights']['momentum_10'],0)
        self.assertNotIn('momentum_10',model['active_features'])
        self.assertEqual(model['feature_counts']['momentum_10']['samples'],10)
        missing=predict(model,training(200))['feature_coverage']
        self.assertEqual(missing['market_available'],0)
        self.assertIn('momentum_10',missing['missing'])
        self.assertIsNone(feature_values(training(200))['momentum_10'])

    def test_direction_alignment_and_fixed_scaling(self):
        row=training(200,v3_momentum_10_bps=5.,v3_momentum_30_bps=-10.,
            v3_momentum_60_bps=15.,v3_trend_gap_bps=15.,v3_volatility_bps=2.,v3_spread=.03)
        up,down=feature_values(row),feature_values({**row,'side':'Down'})
        for feature in ('momentum_10','momentum_30','momentum_60','trend_gap'):
            self.assertEqual(up[feature],-down[feature])
        self.assertEqual(up['volatility'],down['volatility'])
        self.assertEqual(up['spread'],1)
        self.assertEqual(up['momentum_10'],1)

    def test_recent_samples_have_more_weight_and_expired_samples_are_removed(self):
        rows=[training(1,settled_at=NOW-100),training(2,settled_at=NOW-72*3600,
            opened_at=NOW-72*3600-300,decision_at=NOW-72*3600-301),
            training(3,settled_at=NOW-31*86400,opened_at=NOW-31*86400-300,decision_at=NOW-31*86400-301)]
        selected,counts=canonical_samples(rows,now=NOW)
        by_id={r['round_id']:r for r in selected}
        self.assertAlmostEqual(by_id['market-2']['weight'],.5)
        self.assertGreater(by_id['market-1']['weight'],.99)
        self.assertEqual(counts['exclusions']['expired'],1)

    def test_fit_is_deterministic_under_input_reordering(self):
        rows=[training(i,v3_momentum_10_bps=(i%7)-3) for i in range(100)]
        a,b=fit_model(rows,now=NOW),fit_model(reversed(rows),now=NOW)
        self.assertEqual(a,b)
        self.assertEqual(set(a['weights']),set(FEATURES))
        self.assertTrue(all(abs(value)<100 for value in a['weights'].values()))


class AdaptiveV3PromotionTests(unittest.TestCase):
    def test_old_versions_future_backfills_and_training_leak_cannot_promote(self):
        rows=[observation(0),observation(1,model_version='main-adaptive-calibration-v2'),
            observation(2,recorded_at=NOW),observation(3,settled_at=NOW),
            observation(4,fee_source='configured-paper'),observation(5,historical_replay=True),
            observation(6,trained_before=NOW),observation(7,label_source='settled_pnl'),
            observation(8,trained_before=None),observation(9,training_fingerprint=""),
            observation(10,training_fingerprint="not-a-training-sha")]
        result=validate_candidate(rows,now=NOW)
        self.assertEqual(result['samples'],1)
        self.assertFalse(result['passed'])
        self.assertEqual(result['state'],'BASELINE_SHADOW')

    def test_duplicate_markets_keep_first_candidate_and_current_market_excluded(self):
        first=observation(0)
        later={**first,'decision_at':first['decision_at']+2,'recorded_at':first['recorded_at']+2,'candidate_score':.5}
        result=validate_candidate([later,first,observation(1)],now=NOW,round_id='observation-1')
        self.assertEqual(result['samples'],1)
        self.assertAlmostEqual(result['candidate_brier'],(.76-1)**2)

    def test_selection_counterfactuals_are_separated_and_include_abstentions(self):
        rows=[observation(0,candidate_score=.6,won=1),observation(1,candidate_score=.6,won=0),
            observation(2,base_score=.6,candidate_score=.7,won=1),
            observation(3,base_score=.6,candidate_score=.7,won=0),observation(4)]
        result=validate_candidate(rows,now=NOW)
        for name in ('missed_winners','prevented_losers','added_winners','added_losers'):
            self.assertEqual(result[name],1)
        self.assertEqual(result['raw_selected'],3)
        self.assertEqual(result['candidate_selected'],3)
        self.assertAlmostEqual(result['raw_quote_utility'],(2/.65-3)/5)
        self.assertIn('not_realized_pnl',result['policy']['utility_basis'])

    def test_simultaneous_settlements_aggregate_before_drawdown(self):
        rows=[observation(0,won=0,all_in_cost=.5,settled_at=NOW-100),
            observation(1,won=1,all_in_cost=.5,settled_at=NOW-100)]
        a=validate_candidate(rows,now=NOW)
        b=validate_candidate(list(reversed(rows)),now=NOW)
        self.assertEqual(a['raw_quote_drawdown'],0)
        self.assertEqual(a,b)

    def test_drawdown_follows_settlement_time_instead_of_entry_order(self):
        rows=[observation(0,won=1,all_in_cost=.5,settled_at=NOW-1),
              observation(1,won=0,all_in_cost=.5,settled_at=NOW-100)]
        result=validate_candidate(rows,now=NOW)
        self.assertEqual(result['raw_quote_drawdown'],1)

    def test_promotion_requires_both_prediction_and_opportunity_improvement(self):
        # Wins raise probability; known losing opportunities are skipped. This
        # deterministic fixture tests promotion mechanics, not strategy evidence.
        rows=[observation(i,won=0 if i%5==0 else 1,
            candidate_score=.64 if i%5==0 else .76) for i in range(60)]
        good=validate_candidate(rows,now=NOW)
        self.assertTrue(good['passed'])
        self.assertEqual(good['state'],'VALIDATED')
        self.assertGreater(good['quote_utility_improvement'],0)
        self.assertLess(good['candidate_quote_drawdown'],good['raw_quote_drawdown'])
        # Improved calibration with unchanged selections is not a claim of
        # better trading: quote utility must also be better on these observations.
        unchanged=[{**r,'candidate_score':.68 if r['won']==0 else .76} for r in rows]
        fallback=validate_candidate(unchanged,now=NOW)
        self.assertGreater(fallback['brier_improvement'],0)
        self.assertEqual(fallback['quote_utility_improvement'],0)
        self.assertFalse(fallback['passed'])

    def test_declining_candidate_falls_back_without_a_sample_wait_claim(self):
        rows=[observation(i,won=0 if i%4==0 else 1,candidate_score=.6) for i in range(60)]
        # Ensure enough candidate selections while deliberately dropping winners.
        for r in rows:
            if r['won']==0:r['candidate_score']=.76
        result=validate_candidate(rows,now=NOW)
        self.assertEqual(result['state'],'FALLBACK')
        self.assertFalse(result['passed'])
        self.assertGreater(result['samples'],40)
        self.assertGreater(result['observation_hours'],24)


if __name__=='__main__':
    unittest.main()
