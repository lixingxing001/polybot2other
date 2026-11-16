import json
from pathlib import Path
import tempfile
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from polybot2other.config import Settings
from polybot2other.models import MarketRound
from polybot2other.optimized_main import OptimizedMainBot
from polybot2other.optimized_reference import SPOT, TWAP
from polybot2other.optimized_fast import (FastMainBot, FastDecisionJournal, POLICY,
    assess_fast_cost, compact_fast_decision, confirm_fast_prediction, fast_settings)


class References:
    def __init__(self, now):
        self.data = [(stamp*1000, 100.2) for stamp in range(int(now)-180, int(now)+1)]
        self.reads = []

    def points(self, channel, start, end):
        self.reads.append((start, end))
        return [(t, p) for t, p in self.data if start <= t <= end]


class FastPolicyTests(unittest.TestCase):
    def setUp(self):
        self.now = 1800000120.
        self.market = MarketRound('btc-updown-5m-1800000000', 'BTC', self.now-120,
                                  self.now+180, 100, up_token='up', down_token='down')
        self.references = References(self.now)
        self.settings = Settings()
        self.store = SimpleNamespace(trades_for_round=lambda _: [], daily_realized_pnl=lambda: 0,
                                    open_trade_count=lambda _: 0, account=lambda: {'cash_balance':100})
        self.context = SimpleNamespace(settings=self.settings, references=self.references,
                                       store=self.store, paper_trading_paused=False)
        self.reference = {'ready':True, 'protocol_verified':True, 'latest':{
            SPOT:{'source_ms':self.now*1000, 'price':100.2}, TWAP:{'source_ms':self.now*1000, 'price':100.1}}}
        self.quotes = {'Up':{'token_id':'up', 'best_bid':.65, 'best_ask':.66,
            'source_ms':self.now*1000, 'received_ms':self.now*1000,
            'asks':[{'price':.66,'size':1000}]}}

    def evaluate(self):
        return FastMainBot.evaluate(self.context, self.market, self.reference, self.quotes, self.now)

    def test_real_forecast_and_shared_history_open_without_new_training(self):
        original = OptimizedMainBot.evaluate(self.context, self.market, self.reference, self.quotes, self.now)
        signal, decision = self.evaluate()
        self.assertEqual(signal.side, 'Up')
        self.assertTrue(decision['fast_allowed'])
        self.assertEqual(decision['confirmation_samples'], 3)
        self.assertEqual(decision['confirmation_span_seconds'], 6)
        self.assertEqual(decision['fee_source'], 'configured-paper')
        self.assertTrue(all(end <= self.now*1000 for _, end in self.references.reads))
        self.assertEqual(self.context.settings, self.settings)
        self.assertEqual(original, OptimizedMainBot.evaluate(self.context, self.market, self.reference, self.quotes, self.now))

    def test_weak_score_filtered_only_in_fast_variant(self):
        prediction = {'up_score':.64,'projected_final_twap':100.2,'forecast_source_ms':self.now*1000}
        self.quotes['Up'].update(best_ask=.55,best_bid=.54,asks=[{'price':.55,'size':1000}])
        with patch('polybot2other.optimized_main.forecast_twap',return_value=prediction):
            self.assertEqual(OptimizedMainBot.evaluate(self.context,self.market,self.reference,self.quotes,self.now)[0].side,'Up')
            signal, d = self.evaluate()
        self.assertIn('WEAK_SIGNAL', signal.reason)
        self.assertFalse(d['fast_allowed'])

    def test_transient_current_strong_score_is_not_enough(self):
        with patch('polybot2other.optimized_fast.forecast_twap',side_effect=[{'up_score':.4},{'up_score':.8}]):
            signal, d = self.evaluate()
        self.assertEqual(signal.side,'NO_TRADE')
        self.assertEqual(d['code'],'FAST_STABILITY')
        self.assertLess(d['confirmation_min_score'],.66)

    def test_repeated_source_timestamp_cannot_count_as_three_confirmations(self):
        self.references.data=[(self.now*1000,100.2)]*10
        result=confirm_fast_prediction(self.references,self.market,self.reference['latest'],
            {'up_score':.8,'forecast_source_ms':self.now*1000},minimum_score=.66)
        self.assertFalse(result['confirmed'])

    def test_historical_forecasts_never_read_later_ticks(self):
        original_points=self.references.points
        predictions=[]
        def forecast(refs, market, latest):
            stamp=latest[SPOT]['source_ms']
            refs.points(SPOT,stamp-120000,stamp)
            predictions.append(stamp)
            return {'up_score':.8}
        with patch('polybot2other.optimized_fast.forecast_twap',side_effect=forecast):
            result=confirm_fast_prediction(self.references,self.market,self.reference['latest'],
                {'up_score':.8,'forecast_source_ms':self.now*1000},minimum_score=.66)
        self.assertTrue(result['confirmed'])
        self.assertEqual(predictions,[self.now*1000-6000,self.now*1000-3000])

    def test_same_market_entry_prohibited_but_other_market_does_not_block(self):
        self.store.trades_for_round=lambda ident: [{'side':'Down'}] if ident==self.market.round_id else []
        self.assertEqual(self.evaluate()[1]['code'],'SAME_ROUND_ENTRY')
        self.store.trades_for_round=lambda ident: []
        self.assertEqual(self.evaluate()[0].side,'Up')

    def test_cost_and_unit_budget_return_are_rechecked(self):
        self.assertTrue(assess_fast_cost(.66,.638)['allowed'])
        self.assertFalse(assess_fast_cost(.9,.879)['allowed'])
        self.assertFalse(assess_fast_cost(.66,.65)['allowed'])
        self.assertFalse(assess_fast_cost(.66,.638,min_edge=.03)['allowed'])
        for cost in (float('nan'),float('inf'),0,1,True):
            self.assertFalse(assess_fast_cost(.8,cost)['allowed'])

    def test_current_block_does_not_keep_prior_allowed_score_or_edge(self):
        self.assertTrue(self.evaluate()[1]['fast_allowed'])
        self.reference['ready']=False
        signal,d=self.evaluate()
        self.assertFalse(d['fast_allowed'])
        self.assertNotIn('expected_edge',d)
        self.assertNotIn('score',d)
        self.assertEqual(d['code'],'REFERENCE_STALE')

    def test_stricter_user_limits_are_preserved_without_mutating_settings(self):
        source=replace(self.settings,min_confidence=.8,min_edge=.04,max_entry_price=.6,
                       max_spread=.01,min_time_left_seconds=40,max_time_left_seconds=200)
        effective=fast_settings(source)
        for key in ('min_confidence','min_edge','max_entry_price','max_spread','min_time_left_seconds','max_time_left_seconds'):
            self.assertEqual(getattr(source,key),getattr(effective,key))
        self.assertEqual(self.settings.max_entry_price,.72)

    def test_stale_book_and_depth_integrity_are_still_required(self):
        self.quotes['Up']['source_ms']-=5000
        self.assertEqual(self.evaluate()[1]['code'],'QUOTE_STALE')
        self.quotes['Up']['source_ms']=self.now*1000
        self.quotes['Up']['asks']=[{'price':.60,'size':1000}]
        self.assertEqual(self.evaluate()[1]['code'],'DEPTH_OUT_OF_SYNC')

    def test_diagnostics_journal_is_bounded_and_removes_opaque_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'fast.jsonl'
            journal=FastDecisionJournal(path,'paper',max_bytes=600)
            for i in range(10):
                journal.record({'round_id':str(i),'reference':{'secret':'not-for-log'},'score':.8,
                                'reason':'x'*10000},force=True)
            self.assertLess(path.stat().st_size,600)
            self.assertTrue(path.with_suffix('.jsonl.1').exists())
            row=json.loads(path.read_text())
            self.assertNotIn('reference',row)
            self.assertLessEqual(len(row['reason']),240)
            with patch.object(Path,'open',side_effect=OSError('disk unavailable')):
                self.assertFalse(journal.record({'round_id':'failure'},force=True))
            self.assertEqual(journal.error,'OSError')


if __name__=='__main__':
    unittest.main()
