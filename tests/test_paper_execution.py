"""Disposable ledgers and deterministic clocks; never signs/posts funded orders."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from polybot2other.config import Settings
from polybot2other.models import MarketRound, Signal
from polybot2other.storage import TradeStore
from polybot2other.shared_market_inputs import MarketInputs
from polybot2other.paper_execution import PaperExecution, ExecutionPolicy, buy_terms, sweep_book


class PaperExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = 1800000100.
        self.clock_patch = patch('time.time', side_effect=lambda: self.now)
        self.clock_patch.start(); self.addCleanup(self.clock_patch.stop)
        self.market = MarketRound('btc-updown-5m-1800000000', 'BTC', 1800000000., 1800000300., 100.,
                                  condition_id='condition', up_token='up', down_token='down')
        self.store = TradeStore(self.root/'paper.sqlite3', 100)
        self.addCleanup(self.store.conn.close)
        self.store.upsert_round(self.market)
        self.info = {'fee_rate':.07, 'fee_exponent':1, 'tick_size':'0.01', 'neg_risk':False}
        rule = {'enabled':False,'stake_dollars':3.,'max_open_trades':5,'max_daily_loss':21.,'take_profit_pct':20.,'stop_loss_pct':40.}
        self.config = {'enabled':False,'revision':4,'strategies':{key:dict(rule) for key in ('MAIN','MAIN_OPTIMIZED','MAIN_ADAPTIVE','MAIN_TP_SL','VALUE_CONFIRM_V1')}}
        self.quote_changes = {}; self.book_age = 0.; self.frame_error = None
        self.signal = Signal('BTC','Up',.8,.4,15.,'unchanged signal')
        self.settings = Settings(db_path=self.store.db_path,market_scout_enabled=False,llm_super_agent_enabled=False,live_trading_runtime_enabled=False)
        self.bot = SimpleNamespace(store=self.store,settings=self.settings,_lock=threading.RLock(),
            paper_trading_paused=False,last_signal=None,_sync_shared_market_inputs=lambda:self.frame())
        self.reader = Mock(side_effect=lambda token:self.book(token))
        self.feed = SimpleNamespace(references=SimpleNamespace(points=lambda *a:[]))
        self.policy = ExecutionPolicy(confirmation_seconds=10,depth_fraction=.5,redemption_seconds=60)
        self.engine = self.make_engine()

    def make_engine(self, strategy='MAIN'):
        return PaperExecution(self.bot,strategy,self.settings,frame_reader=lambda:self.frame(),
            info_reader=Mock(return_value=self.info),book_reader=self.reader,config_reader=lambda:self.config,
            feed=self.feed,policy=self.policy,clock=lambda:self.now)

    def book(self, token='up'):
        book = {'token_id':token,'best_bid':.39,'best_ask':.4,'bid_size':1000,'ask_size':1000,
            'source_ms':self.now*1000-self.book_age,'received_ms':self.now*1000-self.book_age,
            'bids':[{'price':.39,'size':1000}],'asks':[{'price':.4,'size':1000}],
            'tick_size':'0.01','min_order_size':5}
        book.update(self.quote_changes)
        return book

    def frame(self):
        reference={'ready':True,'protocol_verified':True,'latest':{'price.crypto':{'price':100.15,'source_ms':self.now*1000},
                   'price.crypto.twap':{'price':100.1,'source_ms':self.now*1000}}}
        return MarketInputs(self.market,reference,{'chainlink':100.15,'chainlink_updated_ms':self.now*1000},
            {'Up':self.book('up'),'Down':self.book('down')},self.now,'snapshot',self.frame_error)

    def queue(self, amount=3, limit=.5):
        self.config['strategies'][self.engine.strategy_id]['stake_dollars']=amount
        self.engine.signals.evaluate=Mock(return_value=(self.signal,limit))
        return self.engine.enqueue_buy(self.market,self.signal,limit,self.config['strategies'][self.engine.strategy_id],4,self.info)

    def advance(self, seconds):
        self.now+=seconds;self.engine.poll()

    def confirm(self):
        self.advance(1.1);self.advance(9)

    def test_budget_is_exact_not_resized_and_reserved_until_confirmation(self):
        self.queue();self.assertEqual(self.store.account()['cash_balance'],97)
        self.assertEqual(self.store.reserved_cash(),3);self.assertEqual(self.store.open_trade_count(),0)
        self.assertEqual(self.store.metrics()['total_equity'],100)
        self.advance(1.1)
        self.assertEqual(self.engine.pending()[0]['phase'],'CONFIRMING')
        self.assertEqual(self.store.account()['cash_balance'],97)
        self.advance(9)
        trade=self.store.open_trades()[0]
        self.assertLessEqual(trade['stake'],3);self.assertGreater(trade['stake'],2.8)
        self.assertAlmostEqual(self.store.account()['cash_balance']+trade['stake'],100,places=6)
        self.assertEqual(self.store.reserved_cash(),0)
        self.assertEqual(self.store.recent_paper_orders(10)[0]['requested_cash'],3)

    def test_insufficient_cash_does_not_shrink_the_order(self):
        with self.store.conn:self.store.conn.execute('UPDATE account SET cash_balance=2')
        with self.assertRaisesRegex(ValueError,'不足'):self.queue()
        self.assertEqual(self.store.account()['cash_balance'],2)
        self.assertEqual(self.store.paper_order_count(),0)

    def test_minimum_size_tick_rounding_and_official_fee_reserve(self):
        terms=buy_terms(3,.509,self.info,self.book())
        self.assertEqual(terms['limit'],.50);self.assertEqual(terms['notional_budget'],2.80)
        with self.assertRaisesRegex(ValueError,'低于最小'):buy_terms(3,.70,self.info,self.book())
        with self.assertRaisesRegex(ValueError,'最小下单量'):buy_terms(3,.5,self.info,{**self.book(),'min_order_size':0})
        with self.assertRaisesRegex(ValueError,'价格精度'):buy_terms(3,.5,self.info,{**self.book(),'tick_size':'.001'})

    def test_partial_arrival_fill_below_order_minimum_is_still_real_partial(self):
        self.queue()
        self.quote_changes={'asks':[{'price':.4,'size':4}]}
        self.confirm()
        trade=self.store.open_trades()[0]
        self.assertEqual(trade['shares'],2)  # 50% of the now-visible depth.
        self.assertEqual(self.store.recent_paper_orders(1)[0]['status'],'PARTIAL')
        self.assertAlmostEqual(trade['stake'],.8336,places=6)
        self.assertAlmostEqual(self.store.account()['cash_balance'],99.1664,places=6)

    def test_price_movement_uses_arrival_book_not_signal_price(self):
        self.queue()
        self.quote_changes={'best_ask':.45,'asks':[{'price':.45,'size':1000}]}
        self.confirm()
        self.assertAlmostEqual(self.store.open_trades()[0]['entry_price'],.45,places=6)

    def test_price_above_limit_cancels_and_does_not_wait_for_later_recovery(self):
        self.queue();self.quote_changes={'best_ask':.6,'asks':[{'price':.6,'size':1000}]}
        self.advance(1.1);self.quote_changes={};self.advance(20)
        self.assertEqual(self.store.open_trade_count(),0);self.assertEqual(self.store.account()['cash_balance'],100)
        self.assertEqual(self.store.recent_paper_orders(1)[0]['status'],'CANCELED')

    def test_multiple_levels_charge_official_curve_per_level(self):
        info={**self.info,'fee_exponent':2,'fee_rate':.25}
        book={**self.book(),'asks':[{'price':.4,'size':2},{'price':.5,'size':5}]}
        fill=sweep_book(book,'BUY',.5,info,notional_budget=2.8)
        self.assertEqual(fill['shares'],6);self.assertAlmostEqual(fill['notional'],2.8)
        self.assertAlmostEqual(fill['fee'],2*.25*(.4*.6)**2+4*.25*(.5*.5)**2,places=6)

    def test_no_reuse_of_pre_submission_or_stale_wrong_token_book(self):
        for changes,age in [({},2500),({'token_id':'old'},0),({},10000)]:
            with self.subTest(changes=changes,age=age):
                self.quote_changes={};self.book_age=0;self.queue()
                self.quote_changes=changes;self.book_age=age
                self.advance(1.1)
                self.assertEqual(self.store.open_trade_count(),0)
                self.assertEqual(self.store.account()['cash_balance'],100)

    def test_round_rollover_and_late_worker_do_not_backfill(self):
        self.queue();self.market=replace(self.market,round_id='next-round')
        self.advance(1.1)
        self.assertEqual(self.store.open_trade_count(),0)
        self.market=replace(self.market,round_id='btc-updown-5m-1800000000')
        self.queue();self.advance(30)
        self.assertEqual(self.store.open_trade_count(),0);self.assertEqual(self.store.account()['cash_balance'],100)

    def test_confirmation_is_idempotent_and_cannot_be_canceled_to_free_cash(self):
        ident=self.queue();self.advance(1.1)
        self.assertEqual(self.store.cancel_paper_order(ident)['canceled'],[])
        self.assertEqual(self.store.cancel_active_paper_orders()['canceled'],[])
        self.store.expire_resting_orders(self.market.ends_at+1)
        self.assertEqual(self.store.reserved_cash(),3)
        self.advance(9);before=dict(self.store.account());self.engine.poll()
        self.assertEqual(self.store.account(),before);self.assertEqual(self.store.open_trade_count(),1)

    def test_restart_cancels_unobserved_arrival_but_preserves_saved_match(self):
        self.queue();self.engine=self.make_engine()
        self.assertEqual(self.store.account()['cash_balance'],100)
        self.queue();self.advance(1.1);self.engine=self.make_engine()
        self.assertEqual(self.store.reserved_cash(),3)
        self.advance(9);self.assertEqual(self.store.open_trade_count(),1)

    def test_official_settlement_waits_for_match_confirmation_and_claim_release(self):
        self.queue();self.advance(1.1)
        self.assertEqual(self.store.settle_round_outcome(self.market.round_id,'Up',self.now),[])
        self.advance(9)
        trade=self.store.open_trades()[0]
        self.store.settle_round_outcome(self.market.round_id,'Up',self.now)
        cash=self.store.account()['cash_balance']
        self.assertAlmostEqual(self.engine.pending_cash(),trade['shares'])
        self.assertAlmostEqual(self.store.metrics()['total_equity'],100+self.store.account()['realized_pnl'],places=6)
        self.advance(59);self.assertEqual(self.store.account()['cash_balance'],cash)
        self.advance(2);self.assertAlmostEqual(self.store.account()['cash_balance'],cash+trade['shares'],places=6)
        self.assertEqual(self.engine.pending_cash(),0)
        before=self.store.account()['cash_balance'];self.engine.poll();self.assertEqual(self.store.account()['cash_balance'],before)

    def test_pause_stops_new_decisions_but_keeps_confirmation_progress(self):
        self.queue();self.bot.paper_trading_paused=True
        self.confirm();self.engine.decide()
        self.assertEqual(self.store.open_trade_count(),1);self.assertEqual(self.engine.last_event['code'],'PAUSED')

    def test_settings_revision_change_aborts_preflight(self):
        def read(token):
            self.config['revision']=5
            return self.book(token)
        self.reader.side_effect=read
        with self.assertRaisesRegex(ValueError,'设置已变化'):self.queue()
        self.assertEqual(self.store.reserved_cash(),0)

    def test_disabled_live_switches_do_not_disable_paper_and_amount_is_shared(self):
        self.engine.signals.evaluate=Mock(return_value=(self.signal,.5))
        self.config['strategies']['MAIN']['stake_dollars']=4
        self.engine.decide()
        self.assertEqual(self.store.reserved_cash(),4)
        self.assertFalse(self.config['enabled']);self.assertFalse(self.config['strategies']['MAIN']['enabled'])

    def test_slots_and_daily_loss_budget_include_existing_positions_and_pending(self):
        self.queue();self.confirm()
        rule=self.config['strategies']['MAIN']
        rule['max_open_trades']=1
        self.assertIn('上限',self.engine._entry_block(self.market,'Down',rule))
        rule['max_open_trades']=5;rule['max_daily_loss']=5
        self.assertIn('亏损额度',self.engine._entry_block(self.market,'Down',rule))

    def test_network_read_failure_exposes_reason_and_never_debits(self):
        self.engine.signals.evaluate=Mock(return_value=(self.signal,.5))
        self.reader.side_effect=TimeoutError('private raw response must not be displayed')
        self.engine.decide()
        self.assertEqual(self.store.account()['cash_balance'],100)
        self.assertIn('TimeoutError',self.engine.last_event['message'])
        self.assertNotIn('private',self.engine.last_event['message'])

    def test_sell_partial_conserves_cost_cash_and_uses_arrival_depth(self):
        self.queue();self.confirm();position=self.store.open_trades()[0]
        self.quote_changes={'best_bid':.6,'best_ask':.61,'bids':[{'price':.6,'size':100}], 'asks':[{'price':.61,'size':100}]}
        self.engine.enqueue_sell(position,self.market)
        cash=self.store.account()['cash_balance']
        self.quote_changes['bids']=[{'price':.6,'size':4}]
        self.advance(1.1);self.assertEqual(self.store.account()['cash_balance'],cash)
        self.advance(9)
        remainder=self.store.open_trades()[0]
        self.assertAlmostEqual(remainder['shares'],position['shares']-2)
        self.assertAlmostEqual(self.store.account()['cash_balance'],cash+1.1664,places=6)
        self.assertAlmostEqual(self.store.metrics()['total_equity'],100+self.store.account()['realized_pnl'],places=6)

    def test_take_profit_cannot_execute_below_net_profit_floor_after_latency(self):
        self.queue();self.confirm();position=self.store.open_trades()[0]
        self.quote_changes={'best_bid':.6,'best_ask':.61,'bids':[{'price':.6,'size':100}], 'asks':[{'price':.61,'size':100}]}
        self.engine.enqueue_sell(position,self.market,'止盈')
        self.quote_changes={'best_bid':.3,'best_ask':.31,'bids':[{'price':.3,'size':100}], 'asks':[{'price':.31,'size':100}]}
        self.advance(1.1)
        self.assertEqual(self.store.open_trades()[0]['shares'],position['shares'])
        self.assertEqual(self.store.recent_paper_orders(1)[0]['status'],'CANCELED')

    def test_cannot_sell_dust_below_minimum_or_reuse_same_depth(self):
        self.queue();self.quote_changes={'asks':[{'price':.4,'size':4}]};self.confirm()
        with self.assertRaisesRegex(ValueError,'最小卖单'):self.engine.enqueue_sell(self.store.open_trades()[0],self.market)
        self.assertEqual(self.store.open_trades()[0]['shares'],2)

    def test_config_fields_and_live_signal_implementations_are_reused(self):
        for key in self.config['strategies']:
            with self.subTest(key=key):
                engine=self.make_engine(key)
                engine.signals.evaluate=Mock(return_value=(Signal('BTC','NO_TRADE',0,0,0,'wait'),None))
                self.config['strategies'][key]['stake_dollars']=3.7
                engine.decide()
                args=engine.signals.evaluate.call_args.args
                self.assertEqual(args[0],key);self.assertEqual(args[1],self.config['strategies'][key])
                self.assertEqual(engine.snapshot()['rules']['stake_dollars'],3.7)

    def test_connection_loss_holds_confirmation_until_data_returns(self):
        self.queue();self.advance(1.1);self.book_age=10000;self.advance(9)
        self.assertEqual(self.store.open_trade_count(),0)
        self.assertEqual(self.store.reserved_cash(),3)
        self.assertEqual(self.engine.last_event['code'],'CONFIRMATION_WAIT')
        self.book_age=0;self.engine.poll()
        self.assertEqual(self.store.open_trade_count(),1);self.assertEqual(self.store.reserved_cash(),0)

    def test_mark_uses_net_sweep_and_refuses_incomplete_depth(self):
        self.queue();self.confirm();row=self.store.open_trades()[0]
        self.engine.info_cache[self.market.round_id]=self.info
        quote=self.book();quote.update(best_bid=.5,bids=[{'price':.5,'size':1000}],best_ask=.6)
        mark=self.engine.mark(row,quote,{})
        self.assertAlmostEqual(mark['exit_value'],row['shares']*(.5-.07*.5*.5),places=6)
        quote['bids']=[{'price':.5,'size':1}]
        stale=self.engine.mark(row,quote,{})
        self.assertEqual(stale['valuation_status'],'cached')
        self.assertEqual(stale['exit_value'],mark['exit_value'])
        self.engine.mark_cache.clear()
        self.assertIsNone(self.engine.mark(row,quote,{})['exit_value'])


class PaperExecutionWiringTests(unittest.TestCase):
    def test_all_four_real_bots_share_saved_rules_and_cannot_call_private_exchange(self):
        from polybot2other.bot import PaperTradingBot
        from polybot2other.managed_live_store import ManagedLiveStore
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            settings=Settings(db_path=root/'main.sqlite3',managed_live_enabled=True,paper_realistic_execution=True,
                managed_live_dir=root/'live',strategy_experiments_enabled=True,
                strategy_experiments_variants='MAIN_TP_SL,MAIN_OPTIMIZED,MAIN_ADAPTIVE,VALUE_CONFIRM_V1',
                strategy_experiments_db_dir=root/'experiments',btc_runtime_settings_path=root/'btc.json',
                market_scout_enabled=False,llm_super_agent_enabled=False,live_trading_runtime_enabled=False)
            store=TradeStore(settings.db_path,100);live=ManagedLiveStore(root/'live/accounts.sqlite3')
            unavailable=MarketInputs(None,{}, {},{},1800000100,'none','等待行情')
            references=SimpleNamespace(points=lambda *a:[],snapshot=lambda *a:{},completed_openings=lambda *a:[])
            exchange=SimpleNamespace(market_info=Mock(side_effect=AssertionError('no metadata needed')),
                rest_book=Mock(side_effect=AssertionError('no book needed')),
                prepare=Mock(side_effect=AssertionError('must never sign')),
                submit=Mock(side_effect=AssertionError('must never submit')))
            manager=SimpleNamespace(store=live,exchange=exchange,market_input_snapshot=lambda:unavailable,
                feed=SimpleNamespace(references=references,polymarket=SimpleNamespace(get_resolution=lambda *a:None)))
            before=live.config()
            with patch('polybot2other.managed_live.ManagedLiveAccounts',return_value=manager):
                bot=PaperTradingBot(settings,store)
            try:
                engines=bot.paper_execution_service.engines
                self.assertEqual(set(engines),{'MAIN','MAIN_TP_SL','MAIN_OPTIMIZED','MAIN_ADAPTIVE','VALUE_CONFIRM_V1'})
                self.assertFalse(getattr(bot.strategy_experiments._bots['MAIN_TP_SL'],'independent_runtime',False))
                for engine in engines.values():
                    self.assertIs(engine.bot.paper_execution,engine)
                    engine.decide();engine.poll()
                    self.assertEqual(engine.snapshot()['strategy_id'],engine.strategy_id)
                    self.assertEqual(engine.store.account()['initial_balance'],100)
                bot._run_strategy_from_state()
                self.assertEqual(live.config(),before)
                self.assertEqual(live.conn.execute('SELECT count(*) FROM orders').fetchone()[0],0)
                exchange.prepare.assert_not_called();exchange.submit.assert_not_called()
            finally:
                store.conn.close();live.conn.close()
                for child in bot.strategy_experiments._bots.values():child.store.conn.close()


if __name__ == '__main__':unittest.main()
