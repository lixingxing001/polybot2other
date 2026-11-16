"""Policy tests use synthetic outcomes, never choose constants by historical PnL."""
import unittest

from polybot2other.adaptive_calibration import (
    calibrate_entry, canonical_samples, HALF_LIFE_SECONDS, MAX_SAMPLE_AGE_SECONDS,
    MAX_DOWNWARD_ADJUSTMENT, MAX_UPWARD_ADJUSTMENT)
from polybot2other.adaptive_calibration import price_bucket, validate_candidate, MODEL_VERSION

NOW = 1_800_000_000.


def samples(n=40, *, won=0, age=60., provenance="optimized_raw", base=.8):
    return [dict(source_key=f"s:{i}", round_id=f"r:{i}", side="Up", base_score=base,
        entry_price=.62, opened_at=NOW-age-300-i, settled_at=NOW-age-i,
        decision_at=NOW-age-301-i, time_left_seconds=120., won=won, provenance=provenance)
        for i in range(n)]


def assess(rows, **kwargs):
    values = dict(base_score=.8, side="Up", now=NOW, round_id="current",
        entry_price=.62, all_in_cost=.65, min_edge=.02, time_left_seconds=120)
    return calibrate_entry(rows, **{**values, **kwargs})


class CalibrationPolicyTests(unittest.TestCase):
    def test_future_settlements_and_current_round_never_affect_decision(self):
        prior = samples(30, won=1)
        result = assess(prior)
        future = samples(100, won=0, age=-500)
        for i, row in enumerate(future):
            row["round_id"] = f"future:{i}"
        same = samples(20)
        for row in same:
            row["round_id"] = "current"
        again = assess(prior+future+same)
        self.assertEqual(again["adaptive_score"], result["adaptive_score"])
        self.assertLess(again["adaptive_training_cutoff"], NOW)
        boundary = samples(1)[0]
        boundary.update(settled_at=NOW,round_id="boundary")
        self.assertEqual(assess([boundary])["adaptive_samples"],0)

    def test_duplicate_market_one_vote_prioritizes_original_raw(self):
        original = samples(20, won=0)
        duplicate = []
        for row in original:
            copy = {**row, "source_key": "v2:"+row["source_key"], "provenance":"adaptive_v2_raw", "won":1}
            duplicate.extend([copy]*8)
        a, b = assess(original), assess(original+duplicate)
        self.assertEqual(a["adaptive_score"], b["adaptive_score"])
        self.assertEqual(a["adaptive_effective_samples"], b["adaptive_effective_samples"])
        self.assertEqual(b["adaptive_unique_market_samples"],20)

    def test_recency_decay_and_aged_evidence_falls_back_without_locking(self):
        recent = assess(samples(30,won=0))
        aged = assess(samples(30,won=0,age=HALF_LIFE_SECONDS*4))
        self.assertLess(recent["adaptive_score"],.8)
        self.assertEqual(aged["adaptive_state"],"BASELINE_FALLBACK")
        self.assertEqual(aged["adaptive_score"],.8)
        self.assertTrue(aged["adaptive_allowed"])
        expired=assess(samples(30,age=MAX_SAMPLE_AGE_SECONDS+1))
        self.assertEqual(expired["adaptive_unique_market_samples"],0)

    def test_contaminated_legacy_adaptive_score_is_never_raw(self):
        rows=samples(40,won=1,provenance="legacy_adaptive")
        result=assess(rows)
        self.assertEqual(result["adaptive_score"],.8)
        self.assertEqual(result["adaptive_excluded_legacy_samples"],40)
        self.assertEqual(result["adaptive_state"],"BASELINE_FALLBACK")

    def test_cost_decision_uses_fees_and_does_not_reverse_direction(self):
        rows=samples(40,won=0)
        low=assess(rows,all_in_cost=.65)
        high=assess(rows,all_in_cost=.72)
        self.assertTrue(low["adaptive_allowed"])
        self.assertFalse(high["adaptive_allowed"])
        self.assertEqual(low["adaptive_score"],high["adaptive_score"])
        self.assertAlmostEqual(low["adaptive_expected_edge"]-high["adaptive_expected_edge"],.07)
        weak=assess(samples(40,base=.55),base_score=.55,entry_price=.4,all_in_cost=.48)
        self.assertLess(weak["adaptive_score"],.5)
        self.assertEqual(weak["candidate_side"],"Up")
        self.assertFalse(weak["adaptive_allowed"])

    def test_bounded_asymmetric_adjustments_and_nonzero_uncertainty(self):
        loss=assess(samples(1000,won=0))
        win=assess(samples(1000,won=1))
        self.assertGreaterEqual(loss["adaptive_delta"],-MAX_DOWNWARD_ADJUSTMENT-1e-12)
        self.assertLessEqual(win["adaptive_delta"],MAX_UPWARD_ADJUSTMENT+1e-12)
        self.assertGreater(win["adaptive_standard_error"],0)
        self.assertEqual(win["adaptive_bucket"],"side+score+price+time")

    def test_unknown_provenance_invalid_label_and_invalid_cost(self):
        rows=samples(3)
        rows[0]["provenance"]="unverified"
        rows[1]["settled_at"]=rows[1]["opened_at"]
        rows[2]["exclusion_reason"]="nonterminal_exit"
        self.assertEqual(canonical_samples(rows,now=NOW)[1]["canonical_samples"],0)
        with self.assertRaises(ValueError):
            assess([],all_in_cost=.5)

    def test_price_bin_ignores_sub_tick_share_rounding(self):
        self.assertEqual(price_bucket(.599999974),price_bucket(.6))
        self.assertNotEqual(price_bucket(.5999),price_bucket(.6))


def forward_observations(n=40, *, won=None, prefix="f", start=NOW-3*86400, step=2400):
    return [dict(round_id=f'{prefix}:{i}',decision_at=start+i*step,settled_at=start+i*step+300,
        recorded_at=start+i*step+1,ends_at=start+i*step+299,base_score=.8,candidate_score=.72,all_in_cost=.6,min_edge=.02,
        won=i%2 if won is None else won,label_source='polymarket_official',model_version=MODEL_VERSION,
        fee_source='official-market')
        for i in range(n)]


class ForwardValidationGateTests(unittest.TestCase):
    def test_missing_or_future_labels_backfills_wrong_versions_do_not_promote(self):
        rows=forward_observations()
        for r in rows:r['settled_at']=NOW+1
        self.assertFalse(validate_candidate(rows,now=NOW)['passed'])
        for r in rows:r['settled_at']=r['decision_at']+300;r['recorded_at']=NOW
        self.assertEqual(validate_candidate(rows,now=NOW)['samples'],0)
        rows=forward_observations()
        for r in rows:r['model_version']='old-policy'
        self.assertEqual(validate_candidate(rows,now=NOW)['samples'],0)
        rows=forward_observations()
        for r in rows:r['fee_source']='configured-paper'
        self.assertEqual(validate_candidate(rows,now=NOW)['samples'],0)
        rows=forward_observations()
        for r in rows:r.update(settled_at=r['decision_at']+10,recorded_at=r['decision_at']+11,ends_at=r['decision_at']+9)
        self.assertEqual(validate_candidate(rows,now=NOW)['samples'],0)

    def test_worse_candidate_falls_back_and_more_duplicate_rows_cannot_promote(self):
        rows=forward_observations(won=1)
        result=validate_candidate(rows*10,now=NOW)
        self.assertEqual(result['samples'],40)
        self.assertEqual(result['state'],'FALLBACK')
        self.assertFalse(result['passed'])

    def test_beneficial_forward_predictions_promote_then_degradation_reverts(self):
        rows=forward_observations(160,start=NOW-4*86400,step=600)
        for i,r in enumerate(rows):r.update(base_score=.86,candidate_score=.78,won=int(i%20<13))
        result=validate_candidate(rows,now=NOW)
        self.assertTrue(result['passed'])
        self.assertGreaterEqual(result['brier_improvement'],.005)
        self.assertGreater(result['paired_brier_lower90'],0)
        self.assertGreater(result['candidate_quote_utility'],0)
        degraded=forward_observations(200,won=1,prefix='new',start=NOW-2*86400,step=600)
        for r in degraded:r.update(base_score=.86,candidate_score=.78)
        result=validate_candidate(rows+degraded,now=NOW)
        self.assertFalse(result['passed']);self.assertEqual(result['state'],'FALLBACK')

    def test_quality_alone_cannot_promote_if_quote_cost_utility_worsens(self):
        rows=forward_observations()
        # The candidate skips wins at expensive quotes, despite improving average Brier.
        for r in rows:
            if r['won']:r['all_in_cost']=.74
        result=validate_candidate(rows,now=NOW)
        self.assertGreater(result['brier_improvement'],.005)
        self.assertLess(result['candidate_quote_utility'],result['raw_quote_utility'])
        self.assertFalse(result['passed'])

    def test_smaller_loss_or_noisy_brier_change_is_not_enough(self):
        rows=forward_observations()
        self.assertFalse(validate_candidate(rows,now=NOW)['passed'])
        # Both have negative quote utility, even though calibration error improves.
        for r in rows:r['all_in_cost']=.7
        result=validate_candidate(rows,now=NOW)
        self.assertLess(result['candidate_quote_utility'],0)
        self.assertFalse(result['passed'])


if __name__=="__main__":unittest.main()
