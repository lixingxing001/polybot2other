import json
from pathlib import Path
import tempfile
import time
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from polybot2other.bot import PaperTradingBot
from polybot2other.config import Settings
from polybot2other.experiments import selected_strategy_variants
from polybot2other.models import MarketRound, Signal
from polybot2other.storage import TradeStore
from polybot2other.value_confirm import ValueConfirmGuard, stake_budget, reference_probability, POLICY_VERSION


class ValueConfirmTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = TradeStore(Path(self.temp.name)/'value.sqlite3',100)
        self.addCleanup(self.store.conn.close)
        self.guard = ValueConfirmGuard(self.store)
        self.now = time.time()
        self.market = MarketRound('btc-updown-5m-1793025300','BTC',self.now-120,self.now+180,100)
        self.signal = Signal('BTC','Up',.8,.25,100,'test')
        self.price = {'chainlink':101., 'chainlink_updated_ms':self.now*1000,
                      'okx':101.1, 'okx_updated_ms':self.now*1000,
                      'okx_basis_median_bps':0., 'okx_basis_samples':10}
        self.quote = {'best_bid':.24,'best_ask':.25,'ask_size':1000,
                      'updated_at_ms':self.now*1000,'asks':[{'price':.25,'size':1000}]}
        self.guard.history.append((self.now*1000-25000,100.8))

    def evaluate(self):
        return self.guard.evaluate(self.market,self.signal,self.price,{'Up':self.quote},self.now)

    def test_valid_entry_has_cost_budget_and_append_only_evidence(self):
        self.assertEqual(self.evaluate().side,'Up')
        first = self.guard.journal.read_text()
        self.price['chainlink_updated_ms'] -= 5000
        self.assertEqual(self.evaluate().side,'NO_TRADE')
        self.assertTrue(self.guard.journal.read_text().startswith(first))
        entries = [json.loads(x) for x in self.guard.journal.read_text().splitlines()]
        self.assertEqual([x['code'] for x in entries],['ALLOW','PRICE_STALE'])
        self.assertEqual(entries[0]['stake_budget'],2)
        self.assertEqual(entries[0]['reference_win_rate'],reference_probability(.8))
        self.assertEqual(entries[0]['policy_version'],POLICY_VERSION)

    def test_chosen_side_quote_age_is_checked(self):
        self.quote['updated_at_ms'] -= 3001
        self.assertEqual(self.evaluate().side,'NO_TRADE')
        self.assertEqual(self.guard.last_decision['code'],'QUOTE_STALE')

    def test_future_price_and_cold_start_are_rejected(self):
        self.price['chainlink_updated_ms'] += 2000
        self.assertEqual(self.evaluate().side,'NO_TRADE')
        self.price['chainlink_updated_ms'] = self.now*1000
        self.guard.history.clear()
        self.assertEqual(self.evaluate().side,'NO_TRADE')
        self.assertEqual(self.guard.last_decision['code'],'WARMUP')

    def test_high_price_does_not_pass_by_heuristic_score(self):
        self.signal = Signal('BTC','Up',.99,.60,100,'high score')
        self.quote.update(best_ask=.60,best_bid=.59)
        self.assertEqual(self.evaluate().side,'NO_TRADE')
        self.assertEqual(self.guard.last_decision['code'],'FEE_EDGE')

    def test_mid_price_candidate_can_pass_calibrated_cost_gate(self):
        self.signal=Signal('BTC','Up',.72,.50,10,'mid price')
        self.quote.update(best_ask=.50,best_bid=.49,asks=[{'price':.50,'size':1000}])
        result=self.evaluate()
        self.assertEqual(result.side,'Up')
        self.assertAlmostEqual(result.confidence,355/569)
        self.assertGreater(self.guard.last_decision['net_reference_edge'],.03)

    def test_three_second_source_limit_and_early_window_are_explicit(self):
        self.market=MarketRound(self.market.round_id,'BTC',self.now-45,self.now+255,100)
        self.price['chainlink_updated_ms']=self.now*1000-2800
        self.assertEqual(self.evaluate().side,'Up')
        self.price['chainlink_updated_ms']=self.now*1000-3001
        self.assertEqual(self.evaluate().side,'NO_TRADE')
        self.assertEqual(self.guard.last_decision['code'],'PRICE_STALE')
        self.assertEqual(self.guard.last_decision['price_age_ms'],3001)

    def test_heuristic_score_is_not_treated_as_win_probability(self):
        self.assertLess(reference_probability(.95),.60)
        self.assertGreater(reference_probability(.72),reference_probability(.95))
        self.assertIsNone(reference_probability(.5))
        self.assertIsNone(reference_probability(float('nan')))

    def test_external_disagreement_and_reversing_momentum_block(self):
        self.price['okx'] = 99
        self.assertEqual(self.evaluate().side,'NO_TRADE')
        self.assertEqual(self.guard.last_decision['code'],'CONFIRMATION')
        self.price['okx'] = 101.1
        self.guard.history.clear()
        self.guard.history.append((self.now*1000-25000,101.2))
        self.assertEqual(self.evaluate().side,'NO_TRADE')
        self.assertEqual(self.guard.last_decision['code'],'MOMENTUM')

    def test_budget_shrinks_with_losses_and_never_exceeds_two(self):
        self.assertEqual(stake_budget({'initial_balance':100,'realized_pnl':-10,'cash_balance':90}),1.8)
        self.assertEqual(stake_budget({'initial_balance':100,'realized_pnl':200,'cash_balance':300}),2)
        self.assertEqual(stake_budget({'initial_balance':100,'realized_pnl':0,'cash_balance':.5}),.5)
        self.assertEqual(stake_budget({'initial_balance':100,'realized_pnl':-19.8,'cash_balance':80.2}),.2)
        self.assertEqual(stake_budget({'initial_balance':100,'realized_pnl':-5.8,'cash_balance':94.2},-5.8),.2)

    def test_risk_stops_have_explicit_reasons(self):
        with patch.object(self.store,'daily_realized_pnl',return_value=-6):
            self.assertEqual(self.evaluate().side,'NO_TRADE')
            self.assertEqual(self.guard.last_decision['code'],'DAILY_STOP')
        with patch.object(self.store,'account',return_value={'initial_balance':100,'cash_balance':80,'realized_pnl':-20}):
            self.assertEqual(self.evaluate().side,'NO_TRADE')
            self.assertEqual(self.guard.last_decision['code'],'TOTAL_STOP')

    def test_previous_trade_blocks_reentry_even_after_restart(self):
        self.store.upsert_round(self.market)
        self.store.place_trade(SimpleNamespace(market=self.market,signal=self.signal,stake_dollars=2))
        self.store.settle_round_outcome(self.market.round_id,'Up',now=self.now)
        self.guard = ValueConfirmGuard(self.store)
        self.guard.history.append((self.now*1000-25000,100.8))
        self.assertEqual(self.evaluate().side,'NO_TRADE')
        self.assertEqual(self.guard.last_decision['code'],'ONE_ENTRY')

    def test_isolated_runner_executes_paper_and_keeps_main_settings(self):
        root=Path(self.temp.name)
        settings=Settings(db_path=root/'main.sqlite3',strategy_experiments_enabled=True,
                          strategy_experiments_db_dir=root/'experiments',market_scout_enabled=False,
                          live_trading_runtime_enabled=False)
        main_store=TradeStore(settings.db_path,100)
        self.addCleanup(main_store.conn.close)
        main=PaperTradingBot(settings,main_store)
        runner=main.strategy_experiments
        self.assertEqual(list(runner._bots),['MAIN_TP_SL','MAIN_OPTIMIZED','VALUE_CONFIRM_V1'])
        new=runner._bots['VALUE_CONFIRM_V1']
        self.addCleanup(new.store.conn.close)
        self.assertEqual(main.settings.stake_dollars,5)
        self.assertEqual(new.settings.stake_dollars,2)
        self.assertIsNone(main.value_confirm_guard)
        self.assertIsNone(new.live_trading)
        self.now=time.time()
        self.price.update(chainlink_updated_ms=self.now*1000,okx_updated_ms=self.now*1000)
        self.quote.update(best_ask=.50,best_bid=.49,asks=[{'price':.50,'size':1000}])
        self.quote['updated_at_ms']=self.now*1000
        new.price_basis_tracker._basis['okx']=[9.900990099]*5
        new.value_confirm_guard.history.append((self.now*1000-25000,100.8))
        main.current_market=self.market
        main.paper_price=dict(self.price)
        main.paper_quotes={'Up':dict(self.quote)}
        stale_price={**self.price,'chainlink_updated_ms':self.now*1000-10000}
        stale_quote={**self.quote,'updated_at_ms':self.now*1000-10000}
        with patch.object(runner,'_settle_pending_official_rounds') as settlement:
            def current_snapshot(round_id):
                self.assertTrue(settlement.called)
                return main._fresh_experiment_market_data(round_id)
            runner.market_data_provider=current_snapshot
            runner.run_from_state(self.market,stale_price,{'Up':stale_quote})
        trades=new.store.open_trades()
        self.assertEqual(len(trades),1)
        self.assertLessEqual(trades[0]['stake'],2)
        self.assertEqual(main.store.open_trades(),[])
        self.assertEqual(new.value_confirm_guard.last_decision['code'],'ALLOW')
        self.assertEqual(new.value_confirm_guard.last_decision['source_price_timestamp_ms'],self.price['chainlink_updated_ms'])
        self.assertEqual(new.store.conn.execute('select execution_mode from paper_orders').fetchone()[0],'PAPER')

    def test_refresh_does_not_mix_two_market_windows(self):
        settings=Settings(db_path=self.store.db_path,market_scout_enabled=False,live_trading_runtime_enabled=False)
        main=PaperTradingBot(settings,self.store)
        main.current_market=self.market
        main.paper_price=dict(self.price)
        main.paper_quotes={'Up':dict(self.quote)}
        self.assertIsNone(main._fresh_experiment_market_data('different-market'))
        current=main._fresh_experiment_market_data(self.market.round_id)
        self.assertEqual(current[0]['chainlink_updated_ms'],self.price['chainlink_updated_ms'])

    def test_new_runner_waits_for_official_settlement(self):
        root=Path(self.temp.name)
        settings=Settings(db_path=root/'main.sqlite3',strategy_experiments_enabled=True,
                          strategy_experiments_db_dir=root/'experiments',market_scout_enabled=False,
                          live_trading_runtime_enabled=False)
        main=PaperTradingBot(settings,TradeStore(settings.db_path,100))
        self.addCleanup(main.store.conn.close)
        runner=main.strategy_experiments
        new=runner._bots['VALUE_CONFIRM_V1']
        self.addCleanup(new.store.conn.close)
        ended=MarketRound('btc-updown-5m-ended','BTC',self.now-601,self.now-301,100)
        new.store.upsert_round(ended)
        new.store.place_trade(SimpleNamespace(market=ended,signal=self.signal,stake_dollars=2))
        new.store.save_price_tick('BTC',99,'test-chainlink',ended.ends_at)
        with patch.object(runner.polymarket,'get_resolution',return_value=None):
            runner.run_from_state(self.market,self.price,{'Up':self.quote})
        self.assertEqual(new.store.trades_for_round(ended.round_id)[0]['status'],'OPEN')
        runner._official_settlement_next_at.clear()
        with patch.object(runner.polymarket,'get_resolution',return_value={'outcome':'Up'}):
            runner.run_from_state(self.market,self.price,{'Up':self.quote})
        trade=new.store.trades_for_round(ended.round_id)[0]
        self.assertEqual(trade['status'],'SETTLED')
        self.assertEqual(trade['settlement_source'],'polymarket_official')

    def test_no_resting_orders_do_not_fetch_an_unneeded_book(self):
        settings=Settings(db_path=self.store.db_path,market_scout_enabled=False,live_trading_runtime_enabled=False)
        bot=PaperTradingBot(settings,self.store)
        with patch.object(bot.polymarket,'get_quotes',side_effect=AssertionError('unexpected network request')):
            bot._manage_resting_orders(self.market,{})

    def test_new_strategy_skips_overlapping_tick(self):
        settings=Settings(db_path=self.store.db_path,market_scout_enabled=False,live_trading_runtime_enabled=False)
        bot=PaperTradingBot(settings,self.store)
        bot.configure_strategy_experiment_variant(selected_strategy_variants('VALUE_CONFIRM_V1')[0])
        entered, release = threading.Event(), threading.Event()
        def hold():
            entered.set()
            release.wait(3)
        with patch.object(bot,'_run_strategy_from_state_inner',side_effect=hold) as run:
            worker=threading.Thread(target=bot._run_strategy_from_state)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                bot._run_strategy_from_state()
                self.assertEqual(run.call_count,1)
            finally:
                release.set()
                worker.join(3)

    def test_retired_accounts_cannot_be_selected(self):
        self.assertEqual([x.variant_id for x in selected_strategy_variants('')],['MAIN_TP_SL','MAIN_OPTIMIZED','VALUE_CONFIRM_V1'])
        with self.assertRaises(ValueError):
            selected_strategy_variants('SINGLE_FAK')


if __name__ == '__main__':
    unittest.main()
