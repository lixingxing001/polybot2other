"""Adaptive V2 integration: disposable ledgers, fake broker, never funded orders."""
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from polybot2other.adaptive_main import AdaptiveDecisionJournal, compact_adaptive_decision, AdaptiveMainBot
from polybot2other.managed_live_signals import LiveSignals
from polybot2other.managed_live_client import fee_amount
from polybot2other.models import Signal
from polybot2other.adaptive_model import AdaptiveModel, MODEL_VERSION
from polybot2other.optimized_main import OptimizedMainBot
import test_paper_execution as paper_fixtures
import test_managed_live as live_fixtures


class CalibrationDouble:
    def __init__(self, score=.7):
        self.score = score
        self.calls = []
        self.observations = []

    def adjust_prediction(self, prediction, market, now):
        raw = prediction['up_score']
        side = 'Up' if raw >= .5 else 'Down'
        return {**prediction, 'base_up_score': raw, 'base_selected_score': raw if side == 'Up' else 1-raw,
                'candidate_side': side, 'adaptive_model_version': MODEL_VERSION, 'adaptive_state': 'AWAITING_COST'}

    def assess_entry(self, prediction, market, now, *, entry_price, all_in_cost, min_edge):
        self.calls.append((prediction.copy(), all_in_cost, min_edge))
        return {**prediction, 'adaptive_score': self.score, 'adaptive_allowed': self.score-all_in_cost>=min_edge,
                'adaptive_reason': '校准后费用优势不足' if self.score-all_in_cost<min_edge else '校准后优势达标',
                'adaptive_expected_edge': self.score-all_in_cost, 'adaptive_effective_samples': 21.5,
                'adaptive_samples': 30, 'adaptive_training_cutoff': now-600, 'adaptive_state': 'ADAPTING',
                'adaptive_delta': self.score-prediction['base_selected_score'], 'decision_at': now,
                'entry_price': entry_price, 'all_in_cost': all_in_cost, 'min_edge': min_edge,
                'up_score': self.score if prediction['candidate_side']=='Up' else 1-self.score}

    def record_observation(self, metadata):
        self.observations.append(metadata.copy())


class AdaptivePaperTests(unittest.TestCase):
    setUp = paper_fixtures.PaperExecutionTests.setUp
    make_engine = paper_fixtures.PaperExecutionTests.make_engine
    book = paper_fixtures.PaperExecutionTests.book
    frame = paper_fixtures.PaperExecutionTests.frame
    advance = paper_fixtures.PaperExecutionTests.advance
    confirm = paper_fixtures.PaperExecutionTests.confirm

    def adaptive(self, score=.7):
        self.bot.adaptive_model = CalibrationDouble(score)
        self.engine = self.make_engine('MAIN_ADAPTIVE')
        self.forecast = {'up_score': .8, 'projected_final_twap': 100.2, 'projected_move_bps': 20.}
        self.patch_forecast = patch('polybot2other.optimized_main.forecast_twap', side_effect=lambda *args: self.forecast.copy())
        self.patch_forecast.start(); self.addCleanup(self.patch_forecast.stop)
        return self.engine

    def evaluate(self, key='MAIN_ADAPTIVE', info=None):
        f=self.frame()
        return self.engine.signals.evaluate(key,self.config['strategies'][key],self.market,f.reference,f.quotes,
            self.now,info or self.info,market_input=f)

    def test_official_nonlinear_fee_and_calibration_gate(self):
        self.adaptive(.43)
        info={**self.info,'fee_rate':.25,'fee_exponent':2}
        signal,limit=self.evaluate(info=info)
        expected=.41+.25*(.41*.59)**2
        self.assertAlmostEqual(self.bot.adaptive_model.calls[-1][1],expected,places=6)
        self.assertEqual(signal.side,'NO_TRADE')
        self.assertIsNone(limit)
        self.assertIn('校准后费用优势不足',signal.reason)
        self.assertEqual(len(self.bot.adaptive_model.observations),1)
        self.assertFalse(self.bot.adaptive_model.observations[0]['adaptive_allowed'])
        self.assertEqual(self.bot.adaptive_model.observations[0]['mode'],'paper')

    def test_adjusted_score_does_not_reapply_raw_confidence_floor(self):
        self.adaptive(.55)
        signal,limit=self.evaluate()
        self.assertEqual(signal.side,'Up')
        self.assertLess(signal.confidence,self.settings.min_confidence)
        self.assertEqual(signal.confidence,.55)
        self.assertEqual(self.engine.signals.last_features['base_selected_score'],.8)
        self.assertAlmostEqual(limit,.41)

    def test_adjustment_never_flips_raw_direction(self):
        self.adaptive(.55)
        self.forecast['up_score']=.2
        self.forecast['projected_final_twap']=99.8
        signal,_=self.evaluate()
        self.assertEqual(signal.side,'Down')
        self.assertEqual(signal.confidence,.55)

    def test_base_warmup_and_raw_confidence_checks_are_retained(self):
        self.adaptive(.9)
        self.forecast['up_score']=.59
        signal,_=self.evaluate()
        self.assertEqual(signal.side,'NO_TRADE')
        self.assertIn('WEAK_SIGNAL',signal.reason)
        self.assertEqual(self.bot.adaptive_model.calls,[])

    def test_optimized_contract_unchanged_and_no_calibrator_call(self):
        self.adaptive(.55)
        f=self.frame();rule=self.config['strategies']['MAIN_OPTIMIZED']
        context=SimpleNamespace(settings=replace(self.settings,stake_dollars=rule['stake_dollars'],
            max_open_trades=rule['max_open_trades'],max_daily_loss=rule['max_daily_loss'],paper_taker_fee_rate=0),
            references=self.feed.references,paper_trading_paused=False,
            store=self.engine.signals.views['MAIN_OPTIMIZED'],strategy_label='主账户优化版',
            adjust_prediction=lambda p,m,n:p)
        expected,features=OptimizedMainBot.evaluate(context,self.market,f.reference,f.quotes,self.now)
        edge=expected.confidence-features['limit_price']-fee_amount(1,features['limit_price'],.07,1)
        expected=replace(expected,reason=f'主账户优化版：实时 TWAP 模型，官方费用后优势 {edge:.4f}')
        actual,limit=self.evaluate('MAIN_OPTIMIZED')
        self.assertEqual(actual,expected)
        self.assertEqual(limit,features['limit_price'])
        self.assertEqual(self.bot.adaptive_model.calls,[])
        self.assertEqual(self.bot.adaptive_model.observations,[])

    def test_shadow_candidate_metadata_never_replaces_active_score(self):
        self.adaptive(.8)
        assess=self.bot.adaptive_model.assess_entry
        def shadow(*args,**kwargs):
            return {**assess(*args,**kwargs),'adaptive_candidate_score':.67,
                    'adaptive_candidate_delta':-.13,'adaptive_candidate_allowed':True,
                    'adaptive_candidate_expected_edge':.24,'adaptive_validation_state':'BASELINE_SHADOW',
                    'adaptive_validation_reason':'等待后续官方结算验证','adaptive_validation_samples':0}
        self.bot.adaptive_model.assess_entry=shadow
        candidate,limit=self.evaluate()
        self.assertEqual(candidate.confidence,.8)
        ident=self.engine.enqueue_buy(self.market,candidate,limit,self.config['strategies']['MAIN_ADAPTIVE'],4,self.info)
        payload=json.loads(self.store.conn.execute('select payload from paper_sim_orders where order_id=?',(ident,)).fetchone()[0])
        self.assertEqual(payload['adaptive_decision']['adaptive_score'],.8)
        self.assertEqual(payload['adaptive_decision']['adaptive_candidate_score'],.67)
        self.assertEqual(payload['adaptive_decision']['adaptive_validation_state'],'BASELINE_SHADOW')
        self.assertEqual(self.bot.adaptive_model.observations[-1]['adaptive_candidate_score'],.67)

    def test_stale_features_cleared_on_frame_error_and_other_strategy(self):
        self.adaptive();self.evaluate()
        self.assertEqual(self.engine.signals.last_features['adaptive_score'],.7)
        self.frame_error='行情重连'
        self.evaluate()
        self.assertNotIn('adaptive_score',self.engine.signals.last_features)
        self.assertEqual(self.engine.signals.last_features['reason'],'行情重连')
        self.evaluate('MAIN_OPTIMIZED')
        self.assertEqual(self.engine.signals.last_features,{})

    def test_final_signal_and_raw_snapshot_bind_to_payload_and_fill(self):
        self.adaptive(.7);candidate,limit=self.evaluate()
        self.bot.adaptive_model.score=.64
        ident=self.engine.enqueue_buy(self.market,candidate,limit,self.config['strategies']['MAIN_ADAPTIVE'],4,self.info)
        payload=json.loads(self.store.conn.execute('select payload from paper_sim_orders where order_id=?',(ident,)).fetchone()[0])
        self.assertEqual(payload['signal']['confidence'],.64)
        evidence=payload['adaptive_decision']
        self.assertEqual(evidence['base_selected_score'],.8)
        self.assertEqual(evidence['adaptive_score'],.64)
        self.assertEqual(evidence['mode'],'paper')
        self.assertEqual(evidence['round_id'],self.market.round_id)
        self.assertEqual(evidence['time_left_seconds'],200)
        self.assertEqual(evidence['fee_exponent'],1)
        self.assertEqual(evidence['fee_source'],'official-market')
        self.assertEqual(self.bot.adaptive_model.observations[-1]['fee_source'],'official-market')
        self.engine.signals.last_features['base_selected_score']=.99
        self.confirm()
        after=json.loads(self.store.conn.execute('select payload from paper_sim_orders where order_id=?',(ident,)).fetchone()[0])
        self.assertEqual(after['adaptive_decision'],evidence)
        trade=self.store.open_trades()[0]
        self.assertEqual(after['trade_id'],trade['id'])
        self.assertEqual(trade['confidence'],.64)
        self.assertEqual(self.bot.last_decision['base_selected_score'],.8)

    def test_confirmed_payload_imports_raw_not_adjusted_confidence(self):
        self.adaptive(.64);candidate,limit=self.evaluate()
        self.engine.enqueue_buy(self.market,candidate,limit,self.config['strategies']['MAIN_ADAPTIVE'],4,self.info)
        self.confirm();self.now=self.market.ends_at+1
        self.store.settle_round_outcome(self.market.round_id,'Up',self.now)
        model=AdaptiveModel(self.root/'isolated-model.sqlite3',self.root/'missing-seed.sqlite3',self.store.db_path)
        self.addCleanup(model.close)
        model.sync(force=True)
        row=dict(model.conn.execute("select * from training_samples where source_name='MAIN_ADAPTIVE'").fetchone())
        self.assertEqual(row['provenance'],'adaptive_v2_raw')
        self.assertEqual(row['base_score'],.8)
        self.assertEqual(row['predicted_score'],.64)
        self.assertEqual(row['confidence'],.64)
        self.assertIsNone(row['exclusion_reason'])

    def test_data_wait_clears_previous_learning_decision(self):
        self.adaptive();self.engine.decide()
        self.assertIn('adaptive_score',self.bot.last_decision)
        self.frame_error='下一期基准未就绪';self.engine.decide()
        self.assertNotIn('adaptive_score',self.bot.last_decision)
        self.assertEqual(self.bot.last_decision['reason'],self.frame_error)

    def test_adaptive_no_reverse_after_fill_even_after_settlement(self):
        self.adaptive();candidate,limit=self.evaluate()
        self.engine.enqueue_buy(self.market,candidate,limit,self.config['strategies']['MAIN_ADAPTIVE'],4,self.info)
        self.assertIn('等待确认',self.engine._entry_block(self.market,'Down',self.config['strategies']['MAIN_ADAPTIVE']))
        self.confirm()
        self.assertIn('禁止同一期反手',self.engine._entry_block(self.market,'Down',self.config['strategies']['MAIN_ADAPTIVE']))
        self.store.settle_round_outcome(self.market.round_id,'Up',self.now)
        self.assertIn('禁止同一期反手',self.engine._entry_block(self.market,'Down',self.config['strategies']['MAIN_ADAPTIVE']))
        original=self.make_engine('MAIN_OPTIMIZED')
        self.assertIsNone(original._entry_block(self.market,'Down',self.config['strategies']['MAIN_OPTIMIZED']))

    def test_snapshot_cannot_reuse_other_round_or_rejected_decision(self):
        self.adaptive();self.evaluate()
        with self.assertRaisesRegex(ValueError,'市场不一致'):
            self.engine.signals.adaptive_snapshot(market=replace(self.market,round_id='next'))
        self.engine.signals.last_features['adaptive_allowed']=False
        with self.assertRaisesRegex(ValueError,'缺少可验证'):
            self.engine.signals.adaptive_snapshot(market=self.market)

    def test_legacy_adapter_calibrates_with_explicit_configured_cost(self):
        model=CalibrationDouble(.7)
        bot=object.__new__(AdaptiveMainBot)
        bot.settings=self.settings
        bot.store=self.store
        bot.adaptive_model=model
        bot._adaptive_legacy_journal=AdaptiveDecisionJournal(self.root/'legacy.jsonl','paper')
        base={'up_score':.8,'base_up_score':.8,'base_selected_score':.8,'candidate_side':'Up',
              'adaptive_model_version':'test-v2','limit_price':.5}
        with patch.object(OptimizedMainBot,'evaluate',return_value=(Signal('BTC','Up',.8,.4,10,'raw'),base)):
            signal,features=bot.evaluate(self.market,{}, {},self.now)
        self.assertEqual(signal.side,'Up')
        self.assertEqual(signal.confidence,.7)
        self.assertEqual(features['fee_source'],'configured-paper')
        self.assertAlmostEqual(model.calls[-1][1],.5+self.settings.paper_taker_fee_rate*.25)
        self.assertIn('简化 Paper 配置费率',signal.reason)
        self.assertEqual(model.observations[0]['base_selected_score'],.8)

    def test_diagnostic_journal_failure_does_not_suspend_order_decisions(self):
        journal=AdaptiveDecisionJournal(self.root/'journal.jsonl','paper')
        with patch.object(Path,'open',side_effect=OSError('not writable')):
            self.assertFalse(journal.record({'round_id':'r','code':'ALLOW'}))
        self.assertEqual(journal.error,'OSError')
        self.assertTrue(journal.record({'round_id':'r','code':'ALLOW'}))
        self.assertIsNone(journal.error)

    def test_journal_bounded_sampled_and_private_fields_filtered(self):
        path=self.root/'journal.jsonl';journal=AdaptiveDecisionJournal(path,'paper',max_bytes=800)
        data={'round_id':'r','code':'ALLOW','state':'candidate','candidate_side':'Up','adaptive_allowed':True,
              'base_selected_score':.8,'adaptive_score':.7,'adaptive_training_cutoff':self.now-100,
              'reference':{'secret':'do-not-write'},'wallet':'do-not-write','password':'do-not-write'}
        self.assertTrue(journal.record(data));self.assertFalse(journal.record(data))
        for i in range(8):
            self.now+=6;journal.record({**data,'round_id':str(i)})
        self.assertLessEqual(path.stat().st_size,800)
        backup=path.with_suffix('.jsonl.1');self.assertTrue(backup.exists());self.assertLessEqual(backup.stat().st_size,800)
        content=path.read_text()+backup.read_text()
        self.assertNotIn('do-not-write',content)
        self.assertIn('"mode":"paper"',content)
        self.assertNotIn('reference',compact_adaptive_decision(data))


class AdaptiveLiveTests(unittest.TestCase):
    setUp=live_fixtures.ManagedLiveTests.setUp
    enable=live_fixtures.ManagedLiveTests.enable
    confirm=live_fixtures.ManagedLiveTests.confirm

    def snapshot(self):
        return {'round_id':self.market.round_id,'candidate_side':'Up','base_selected_score':.8,
                'adaptive_score':.73,'adaptive_allowed':True,'adaptive_model_version':'test-v2',
                'decision_at':self.market.started_at+60,'mode':'live'}

    def test_live_submission_metadata_is_immutable_and_uses_fresh_signal(self):
        self.enable('MAIN_ADAPTIVE')
        self.manager.signals.adaptive_snapshot=Mock(return_value=self.snapshot())
        self.manager.signals.evaluate=Mock(side_effect=[(Signal('BTC','Up',.8,.4,10,'earlier'),.5),
                                                       (Signal('BTC','Up',.73,.4,10,'fresh'),.5)])
        self.manager.tick()
        order=self.store.active_orders('MAIN_ADAPTIVE')[0]
        self.assertEqual(order['reason'],'fresh')
        self.assertEqual(json.loads(order['adaptive_decision_json']),self.snapshot())
        self.store.record_submission(order['id'],{'accepted':True,'signature':'never-save'})
        after=self.store.order(order['id'])
        self.assertEqual(after['adaptive_decision_json'],order['adaptive_decision_json'])
        self.assertNotIn('signature',after['submission_json'])
        self.confirm(order,10,.4)
        rule=self.store.config()['strategies']['MAIN_ADAPTIVE']
        self.assertIn('禁止同一期反手',self.manager._entry_restriction('MAIN_ADAPTIVE',rule,self.market,'Down'))
        with self.assertRaisesRegex(ValueError,'禁止同一期反手'):
            self.store.prepare_order('next','MAIN_ADAPTIVE',self.market,'down','Down','BUY',.5,cash=5)

    def test_live_mode_does_not_feed_shadow_training_and_defaults_remain_off(self):
        before=self.store.config();model=CalibrationDouble(.7)
        signals=LiveSignals(self.settings,self.store,self.feed,self.manager.available_cash,lambda:{},adaptive_model=model)
        base={'up_score':.8,'base_up_score':.8,'base_selected_score':.8,'candidate_side':'Up','adaptive_model_version':'test-v2','limit_price':.5}
        frame=SimpleNamespace(error=None,market=self.market,reference={},quotes={},observed_at=self.market.started_at+60)
        with patch('polybot2other.managed_live_signals.OptimizedMainBot.evaluate',return_value=(Signal('BTC','Up',.8,.4,10,'raw'),base)):
            signal,_=signals.evaluate('MAIN_ADAPTIVE',before['strategies']['MAIN_ADAPTIVE'],self.market,{}, {},frame.observed_at,self.exchange.market_info(self.market),market_input=frame)
        self.assertEqual(signal.confidence,.7)
        self.assertEqual(model.observations,[])
        self.assertEqual(self.store.config(),before)
        self.assertFalse(before['strategies']['MAIN_ADAPTIVE']['enabled'])
        self.assertEqual(self.exchange.submitted,[])
