"""Feed alignment tests use disposable ledgers, fake feeds and no real broker."""
from copy import deepcopy
from dataclasses import asdict,replace
from types import SimpleNamespace
import json
import threading
import time
import unittest
import urllib.request
from unittest.mock import Mock,patch

from polybot2other.bot import PaperTradingBot
from polybot2other.storage import TradeStore
from polybot2other.managed_live_signals import LiveSignals
from polybot2other.optimized_reference import SPOT,TWAP
from polybot2other.shared_market_inputs import SharedMarketInputs
from polybot2other.strategy import RealBtcFiveMinuteStrategy,input_from_snapshot
from polybot2other.models import Signal,TradeIntent
from polybot2other.execution import simulate_fak_buy
from polybot2other.web import DashboardServer,Handler
import test_managed_live as fixtures


class SharedInputsTests(unittest.TestCase):
    setUp=fixtures.ManagedLiveTests.setUp

    def aligned(self,experiments=False):
        self.now=float(int(time.time()))
        clock=patch('time.time',return_value=self.now);clock.start();self.addCleanup(clock.stop)
        self.btc=100.15;self.spot_age=100.;self.quote_age=100.;self.ready=True;self.verified=True
        self.quote_changes={}
        self.feed.references=SimpleNamespace(reset_connection=Mock(),points=Mock(return_value=[]),
                                             completed_openings=Mock(return_value=[]))
        self.feed.polymarket.find_current_btc_5m_market=Mock(side_effect=AssertionError('No legacy market fetch'))
        def observation():
            quotes={side:{'token_id':token,'best_bid':.39,'best_ask':.4,'bid_size':1000,'ask_size':1000,
                          'source_ms':self.now*1000-self.quote_age,'received_ms':self.now*1000-self.quote_age,
                          'updated_at_ms':self.now*1000,
                          'bids':[{'price':.39,'size':1000}],'asks':[{'price':.4,'size':1000}]}
                    for side,token in [('Up',self.feed.market.up_token),('Down',self.feed.market.down_token)]}
            for side,changes in self.quote_changes.items():quotes[side].update(changes)
            ref={'ready':self.ready,'protocol_verified':self.verified,'latest':{
                SPOT:{'price':self.btc,'source_ms':self.now*1000-self.spot_age,'received_ms':self.now*1000-self.spot_age},
                TWAP:{'price':100.05,'source_ms':self.now*1000-self.spot_age,'received_ms':self.now*1000-self.spot_age}}}
            return self.feed.market,ref,quotes,self.now
        self.feed.observation=observation
        self.manager.signals=LiveSignals(self.settings,self.store,self.feed,self.manager.available_cash,self.manager._external_prices)
        settings=replace(self.settings,strategy_experiments_enabled=experiments,
                         strategy_experiments_variants='MAIN_OPTIMIZED,MAIN_ADAPTIVE,MAIN_TP_SL,VALUE_CONFIRM_V1',
                         strategy_experiments_db_dir=self.root/'experiments')
        self.paper_store=TradeStore(self.root/'original-paper.sqlite3',100);self.addCleanup(self.paper_store.conn.close)
        with patch('polybot2other.managed_live.ManagedLiveAccounts',return_value=self.manager):
            self.paper=PaperTradingBot(replace(settings,db_path=self.paper_store.db_path),self.paper_store)
        self.manager.owner=self.paper
        self.paper._maybe_place_trade=Mock(return_value=[])
        self.paper._run_strategy_experiments=Mock()
        if experiments:
            for bot in self.paper.strategy_experiments._bots.values():
                self.addCleanup(bot.store.conn.close)
                if getattr(bot,'baseline',None):self.addCleanup(bot.baseline.close)
        return self.paper

    def compare_main(self):
        self.paper._run_strategy_from_state()
        frame=self.manager.market_input_snapshot()
        signal,_=self.manager.signals.evaluate('MAIN',self.store.config()['strategies']['MAIN'],frame.market,frame.reference,
                                              frame.quotes,frame.observed_at,fixtures.exchange_info(),market_input=frame)
        for field in ('side','confidence','entry_price','move_bps','reason'):
            self.assertEqual(self.paper.last_signal[field],getattr(signal,field),field)
        self.assertEqual(self.paper._last_market_input.snapshot_id,frame.snapshot_id)
        return signal,frame

    def test_same_main_input_and_signal_even_with_live_switches_off(self):
        self.aligned();before=self.store.config();signal,frame=self.compare_main()
        self.assertEqual(signal.side,'Up');self.assertFalse(before['enabled'])
        self.assertEqual(self.store.config(),before);self.assertEqual(self.exchange.submitted,[])
        self.assertEqual(self.paper.paper_price,frame.price)
        self.assertEqual(self.paper.paper_quotes,frame.quotes)

    def test_historical_opening_mismatch_now_uses_verified_target_in_both(self):
        self.aligned();self.btc=77103.27
        self.feed.market=replace(self.market,target_price=77137.21198263785)
        self.paper.current_market=replace(self.market,target_price=77071.20850575119)
        signal,frame=self.compare_main()
        legacy=RealBtcFiveMinuteStrategy(self.settings).signal(input_from_snapshot(
            replace(self.feed.market,target_price=77071.20850575119),{'price':frame.price,'quotes':frame.quotes},now_ts=frame.observed_at))
        self.assertEqual(legacy.side,'Up');self.assertEqual(signal.side,'Down')
        self.assertEqual(self.paper.current_market.target_price,77137.21198263785)

    def test_fresh_auxiliary_price_cannot_disguise_old_chainlink(self):
        self.aligned();self.spot_age=60000
        self.paper._ingest_backend_spot_price({'okx':101.,'okx_updated_ms':self.now*1000})
        signal,frame=self.compare_main()
        self.assertEqual(signal.side,'NO_TRADE');self.assertEqual(signal.reason,'Chainlink 参考价未就绪')
        self.assertEqual(frame.price['chainlink_updated_ms'],self.now*1000-60000)
        self.assertEqual(self.exchange.submitted,[])

    def test_selected_stale_future_or_wrong_token_book_is_rejected_in_both(self):
        self.aligned()
        for changes in [{'source_ms':self.now*1000-10000}, {'received_ms':self.now*1000-5000},
                        {'source_ms':self.now*1000+1000},{'token_id':'previous-round-token'}, {'best_bid':.6}]:
            with self.subTest(changes=changes):
                self.quote_changes={'Up':changes};signal,frame=self.compare_main()
                self.assertEqual(signal.side,'NO_TRADE')
                if 'source_ms' in changes:self.assertEqual(frame.quotes['Up']['updated_at_ms'],changes['source_ms'])

    def test_browser_legacy_rest_and_rtds_cannot_replace_verified_inputs(self):
        self.aligned();_,before=self.compare_main()
        result=self.paper.ingest_live_snapshot({'market':{'slug':self.market.round_id,'target_price':1},
            'price':{'chainlink':1,'chainlink_updated_ms':self.now*1000},'quotes':{'Up':{'best_ask':.01}}})
        self.assertEqual(result['ignored_snapshot'],'shared_verified_feed_authoritative')
        self.paper._ingest_backend_chainlink_price({'chainlink':1,'chainlink_updated_ms':self.now*1000})
        self.paper._ingest_backend_clob_ws_quotes(self.market,{'Up':{'best_ask':.01}})
        with patch.object(self.paper.market_data_polymarket,'get_quotes',side_effect=AssertionError('Legacy REST called')):
            self.paper._backend_quote_snapshot(self.market)
        _,after=self.compare_main();self.assertEqual(before.price,after.price);self.assertEqual(before.quotes,after.quotes)

    def test_unavailable_shared_feed_does_not_fall_back_to_old_market(self):
        self.aligned();self.compare_main()
        self.paper._shared_market_provider=Mock(side_effect=TimeoutError)
        self.assertIsNone(self.paper._refresh_market());self.assertIsNone(self.paper.current_market)
        self.assertEqual(self.paper.paper_quotes,{});self.assertIn('统一行情暂不可用',self.paper.last_signal['reason'])
        self.feed.polymarket.find_current_btc_5m_market.assert_not_called()

    def test_readiness_changes_and_round_rollover_reach_both_consumers(self):
        self.aligned();_,old=self.compare_main();self.verified=False
        signal,waiting=self.compare_main();self.assertEqual(signal.side,'NO_TRADE');self.assertNotEqual(old.snapshot_id,waiting.snapshot_id)
        self.verified=True;self.feed.market=replace(self.market,round_id='new-round',up_token='new-up',down_token='new-down')
        signal,frame=self.compare_main();self.assertEqual(frame.market.round_id,'new-round')
        self.assertEqual(self.paper.paper_quotes['Up']['token_id'],'new-up');self.assertEqual(signal.side,'Up')

    def test_snapshot_copies_do_not_mutate_the_collector(self):
        self.aligned();a=self.manager.market_input_snapshot();a.quotes['Up']['bids'][0]['size']=0;a.price['chainlink']=1
        b=self.manager.market_input_snapshot();self.assertEqual(b.price['chainlink'],self.btc)
        self.assertEqual(b.quotes['Up']['bids'][0]['size'],1000)

    def test_all_retained_experiments_borrow_feed_without_duplicate_collectors(self):
        self.aligned(experiments=True);runner=self.paper.strategy_experiments
        self.assertEqual(set(runner._bots),{'MAIN_OPTIMIZED','MAIN_ADAPTIVE','MAIN_TP_SL','VALUE_CONFIRM_V1'})
        for bot in runner._bots.values():
            config=asdict(bot.settings);frame=bot._sync_shared_market_inputs()
            self.assertEqual(frame.snapshot_id,self.manager.market_input_snapshot().snapshot_id)
            self.assertIs(bot.polymarket,self.feed.polymarket);self.assertEqual(asdict(bot.settings),config)
        optimized=runner._bots['MAIN_OPTIMIZED'];exit_bot=runner._bots['MAIN_TP_SL']
        self.assertIs(optimized.references,self.feed.references)
        with patch('threading.Thread') as thread:
            optimized._start_workers();self.assertEqual(thread.call_count,1)
            self.assertIsNone(optimized._reference_thread);self.assertIsNone(optimized._book_thread)
            thread.reset_mock();exit_bot._start_workers();self.assertEqual(thread.call_count,2);self.assertIsNone(exit_bot._book_thread)
        with patch.object(optimized.reference_feed,'close') as close:
            optimized.request_pause();close.assert_not_called()
        self.feed.references.reset_connection.assert_not_called()
        self.assertEqual(self.manager.market_input_snapshot().error,None)

    def test_paper_refreshes_after_other_io_before_evaluating(self):
        self.aligned();self.paper._refresh_market()
        self.paper._manage_resting_orders=Mock(side_effect=lambda *args:setattr(self,'btc',99.85))
        signal,frame=self.compare_main();self.assertEqual(signal.side,'Down')
        self.assertEqual(self.paper.paper_price['chainlink'],99.85)

    def test_status_and_cached_status_overlay_use_one_frame_for_both_views(self):
        self.aligned();self.paper._refresh_market()
        snapshot=self.paper.snapshot()
        for data in [snapshot]:
            runtime=data['runtime'];live=runtime['managed_live']['market_data']
            self.assertEqual(runtime['market_input_alignment']['snapshot_id'],live['snapshot_id'])
            self.assertEqual(runtime['paper_price']['chainlink'],live['spot'])
            self.assertEqual(runtime['current_market']['target_price'],live['market']['target_price'])
            self.assertEqual(runtime['paper_quotes']['Up']['source_ms'],live['quotes']['Up']['source_ms'])
        self.btc=99.85
        refreshed=self.paper.refresh_status_runtime_overlay(snapshot);runtime=refreshed['runtime']
        self.assertEqual(runtime['paper_price']['chainlink'],99.85)
        self.assertEqual(runtime['managed_live']['market_data']['spot'],99.85)
        self.assertEqual(runtime['market_input_alignment']['snapshot_id'],runtime['managed_live']['market_data']['snapshot_id'])
        self.assertEqual(self.exchange.submitted,[])

    def test_auxiliary_updates_remain_shared_but_cannot_inject_a_reference_price(self):
        self.aligned()
        self.paper._ingest_backend_spot_price({'okx':100.2,'okx_updated_ms':self.now*1000,
                                             'chainlink':1,'chainlink_updated_ms':self.now*1000,'target_price':1})
        signal,frame=self.compare_main()
        self.assertEqual(frame.price['okx'],100.2);self.assertEqual(frame.price['chainlink'],self.btc)
        self.assertEqual(frame.market.target_price,100)

    def test_paper_and_status_reads_do_not_change_live_basis_sampling(self):
        self.aligned();source=self.manager.signals.market_inputs
        self.paper._ingest_backend_spot_price({'okx':100.2,'okx_updated_ms':self.now*1000})
        self.manager.market_input_snapshot(advance_basis=True)
        previous=list(source.basis['okx'])
        for offset in range(10):
            self.paper._auxiliary_prices.update(okx=100.3+offset*.01,okx_updated_ms=self.now*1000-50+offset)
            self.paper._refresh_market();self.paper.snapshot()
        self.assertEqual(list(source.basis['okx']),previous)
        self.manager.market_input_snapshot(advance_basis=True)
        self.assertEqual(len(source.basis['okx']),len(previous)+1)

    def test_root_does_not_start_a_second_authoritative_book_subscription(self):
        self.aligned();self.manager.start=Mock();self.manager.set_market_paused=Mock()
        with patch('threading.Thread'):
            self.paper.start()
        self.assertIsNone(self.paper._clob_ws_thread)
        self.manager.start.assert_called_once()
        self.assertIsNotNone(self.paper._rtds_ws_thread)  # Auxiliary Binance only; Chainlink is ignored.

    def test_aligned_settlement_waits_for_official_result_instead_of_current_spot(self):
        self.aligned();self.paper._refresh_market()
        fill=simulate_fak_buy(TradeIntent(self.market,Signal('BTC','Up',.8,.4,10,'test entry'),5),
                              {'best_ask':.4,'asks':[{'price':.4,'size':1000}]}).fills[0]
        ident=self.paper_store.place_fills([fill])[0]
        self.paper.paper_price={'chainlink':110,'chainlink_updated_ms':(self.market.ends_at+10)*1000}
        self.feed.polymarket.get_resolution.return_value=None
        self.paper._settle_due(self.market.ends_at+10)
        self.assertEqual(self.paper_store.open_trades()[0]['id'],ident)
        self.feed.polymarket.get_resolution.return_value={'outcome':'Down','target_price':100,'final_price':99}
        self.paper._settle_due(self.market.ends_at+20)
        settled=self.paper_store.recent_trades(1)[0]
        self.assertEqual(settled['id'],ident);self.assertEqual(settled['pnl'],-5)
        self.assertEqual(settled['settlement_source'],'polymarket_official')

    def test_http_status_contract_and_browser_post_are_aligned_without_orders(self):
        self.aligned();self.paper._refresh_market()
        server=DashboardServer(('127.0.0.1',0),Handler,self.paper)
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.05},daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        base=f'http://127.0.0.1:{server.server_port}'
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(base+'/api/status',timeout=3) as response:payload=json.load(response)
        r=payload['runtime'];live=r['managed_live']['market_data']
        self.assertEqual(r['market_input_alignment']['snapshot_id'],live['snapshot_id'])
        self.assertEqual(r['paper_price']['chainlink'],live['spot'])
        finished=threading.Event();original=self.paper.ingest_live_snapshot
        def ingest(value):
            try:return original(value)
            finally:finished.set()
        self.paper.ingest_live_snapshot=ingest
        request=urllib.request.Request(base+'/api/live-snapshot',method='POST',
            headers={'Content-Type':'application/json'},data=json.dumps({'price':{'chainlink':1},'quotes':{'Up':{'best_ask':.01}}}).encode())
        with opener.open(request,timeout=3) as response:self.assertTrue(json.load(response)['ok'])
        self.assertTrue(finished.wait(2))
        self.assertEqual(self.paper.paper_price['chainlink'],self.btc)
        self.assertEqual(self.paper_store.recent_trade_count(),0);self.assertEqual(self.exchange.submitted,[])


if __name__=='__main__':unittest.main()
