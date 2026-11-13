"""Verified shared market collector and the existing live strategy adapters."""
from dataclasses import replace
from collections import deque
from pathlib import Path
import math
import statistics
import time
from types import SimpleNamespace
import httpx

from .models import Signal
from .storage import TradeStore
from .strategy import RealBtcFiveMinuteStrategy, input_from_snapshot
from .optimized_main import OptimizedMainBot
from .optimized_reference import SPOT
from .value_confirm import ValueConfirmGuard, entry_limit
from .managed_live_client import fee_amount
from .buffered_ws import BufferedWebSocket
from .managed_live_book import ReliableLiveBooks
from .shared_market_inputs import SharedMarketInputs


class LiveMarketFeed(OptimizedMainBot):
    """Use the verified reference/book readers without placing any Paper orders."""
    def __init__(self, settings, path, book_reader=None):
        settings = replace(settings, db_path=Path(path)/"market.sqlite3", strategy_experiments_enabled=False,
                           live_trading_runtime_enabled=False, managed_live_enabled=False,
                           market_scout_enabled=False, llm_super_agent_enabled=False)
        store = TradeStore(Path(":memory:"), 1)
        super().__init__(settings, store)
        self.paper_trading_paused = True
        self.market_http=httpx.Client(timeout=httpx.Timeout(6,connect=3),follow_redirects=False)
        self.polymarket._get_json=self._market_json
        self.reference_feed.socket_factory=BufferedWebSocket.connect
        self.books=ReliableLiveBooks(self._current_book_market,self._publish_book_quotes,self._book_quotes,book_reader)
        self.research_observer=None

    def _current_book_market(self):
        with self._lock:return self.current_market

    def _book_quotes(self):
        with self._lock:return {k:dict(v) for k,v in self.paper_quotes.items()}

    def _publish_book_quotes(self,market,quotes):
        with self._lock:
            current=self.current_market
            if (self._stop.is_set() or current is None or current.ends_at<=time.time() or current.round_id!=market.round_id
                    or current.up_token!=market.up_token or current.down_token!=market.down_token):return False
            changed=False
            for side,quote in quotes.items():
                token=current.up_token if side=="Up" else current.down_token
                if str(quote.get("token_id"))!=str(token):continue
                previous=self.paper_quotes.get(side,{})
                if previous.get("source_ms",0)>quote["source_ms"]:continue
                for target in (self.paper_quotes,self.execution_quotes,self.latest_quotes):target[side]=dict(quote)
                changed=True
            return changed

    def _book_loop(self):
        self.books.run(self._stop)

    def request_pause(self):
        super().request_pause()
        self.books.request_resync("行情已暂停")

    def recover_after_interruption(self):
        self.references.reset_connection("resuming")
        self.reference_feed.close()
        self.books.request_resync("运行中断后重新同步")
        with self._lock:self.paper_quotes={};self.execution_quotes={};self.latest_quotes={}

    def health_snapshot(self):
        return self.books.health()

    def _market_json(self, url, params=None):
        response=self.market_http.get(url,params=params)
        if response.status_code==404:return {}
        response.raise_for_status()
        if self.research_observer is not None:
            try:self.research_observer('MARKET_METADATA',{'url':url,'params':params,'response':response.json()},int(time.time()*1000),raw_text=response.text)
            except Exception:pass
        return response.json()

    def optimized_tick(self):
        with self._decision_lock:
            self._market(time.time())
            self._resolve(time.time())

    def observation(self):
        with self._lock:
            market = self.current_market
            quotes = {k:dict(v) for k,v in self.paper_quotes.items()}
        now = time.time()
        return market, self.references.snapshot(now*1000), quotes, now


class LiveRiskView:
    """Expose only real strategy accounting to the shared signal functions."""
    def __init__(self, store, strategy_id, cash):
        self.store=store;self.strategy_id=strategy_id;self.cash=cash
        self.db_path=store.path.parent/(strategy_id.lower()+"-signal.sqlite3")

    def account(self):
        return {"initial_balance":0,"realized_pnl":sum(p["realized_pnl"] for p in self.store.positions(self.strategy_id)),
                "cash_balance":max(0,self.cash())}

    def daily_realized_pnl(self):
        return self.store.daily_pnl(self.strategy_id)

    def open_trade_count(self, symbol="BTC"):
        return self.store.risk_usage(self.strategy_id)["slots"]

    def trades_for_round(self, round_id):
        return ([p for p in self.store.positions(self.strategy_id) if p["round_id"]==round_id]
                +self.store.active_orders(self.strategy_id,round_id))


class LiveSignals:
    def __init__(self, settings, store, feed, cash, external_prices, adaptive_model=None, adaptive_model_error=None):
        self.settings=settings;self.store=store;self.feed=feed;self.cash=cash;self.external_prices=external_prices
        self.views={key:LiveRiskView(store,key,cash) for key in ("MAIN","MAIN_OPTIMIZED","MAIN_ADAPTIVE","VALUE_CONFIRM_V1","MAIN_TP_SL")}
        self.adaptive_model=adaptive_model
        self.adaptive_model_error=adaptive_model_error
        self.last_features={}
        self.value_settings={"stake_dollars":2.0}
        self.value=ValueConfirmGuard(self.views["VALUE_CONFIRM_V1"],settings.paper_taker_fee_rate,
                                    budget_calculator=lambda account,daily:min(self.value_settings["stake_dollars"],account["cash_balance"]))
        self.market_inputs=SharedMarketInputs(feed,external_prices)

    def evaluate(self, strategy_id, config, market, reference, quotes, now, info, *, market_input=None):
        def blocked(reason):
            return Signal("BTC","NO_TRADE",0,0,0,reason),None
        frame=market_input or self.market_inputs.build(market,reference,quotes,now)
        if frame.error:return blocked(frame.error)
        market,reference,quotes,now=frame.market,frame.reference,frame.quotes,frame.observed_at
        settings=replace(self.settings,stake_dollars=config["stake_dollars"],max_open_trades=config["max_open_trades"],
                         max_daily_loss=config["max_daily_loss"] or 1e100,paper_taker_fee_rate=info["fee_rate"])
        if strategy_id in {"MAIN_OPTIMIZED", "MAIN_ADAPTIVE"}:
            if strategy_id == "MAIN_ADAPTIVE" and self.adaptive_model is None:
                detail=self.adaptive_model_error or "模型文件不可用"
                return blocked(f"主账户自适应成长版：自适应模型未就绪（{detail}）")
            adjust_prediction = None
            if strategy_id == "MAIN_ADAPTIVE" and self.adaptive_model is not None:
                adjust_prediction = self.adaptive_model.adjust_prediction
            context=SimpleNamespace(settings=replace(settings,paper_taker_fee_rate=0),references=self.feed.references,
                                    paper_trading_paused=False,
                                    store=self.views.get(strategy_id, self.views["MAIN_OPTIMIZED"]),
                                    strategy_label=("主账户自适应成长版" if strategy_id == "MAIN_ADAPTIVE" else "主账户优化版"),
                                    adjust_prediction=adjust_prediction or (lambda prediction, _market, _now: prediction))
            signal,features=OptimizedMainBot.evaluate(context,market,reference,quotes,now)
            self.last_features=dict(features)
            limit=features.get("limit_price")
            if signal.side in ("Up","Down") and limit is not None:
                edge=signal.confidence-limit-fee_amount(1,limit,info["fee_rate"],info["fee_exponent"])
                if edge<max(.02,settings.min_edge):return blocked("按官方费用曲线计算后的模型优势不足")
                label = "主账户自适应成长版" if strategy_id == "MAIN_ADAPTIVE" else "主账户优化版"
                signal=replace(signal,reason=f"{label}：实时 TWAP 模型，官方费用后优势 {edge:.4f}")
            return signal,limit
        price=frame.price
        data=frame.strategy_input()
        signal=RealBtcFiveMinuteStrategy(settings).signal(data)
        if signal.side in ("Up","Down"):
            error=frame.quote_error(signal.side)
            if error:return blocked(error)
        if strategy_id=="VALUE_CONFIRM_V1":
            self.value_settings=config
            candidate_limit=min(.65,signal.entry_price+.01)
            curvature=candidate_limit*(1-candidate_limit)
            self.value.fee_rate=info["fee_rate"]*curvature**(info["fee_exponent"]-1)
            self.value.daily_loss_limit=config["max_daily_loss"];self.value.position_limit=config["max_open_trades"]
            signal=self.value.evaluate(market,signal,price,quotes,now)
            return signal,entry_limit(signal,self.value.fee_rate) if signal.side in ("Up","Down") else None
        limit=round(min(settings.max_entry_price,max(signal.entry_price,signal.confidence-settings.min_edge)),4)
        return signal,limit
