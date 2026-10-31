"""Live-only market inputs and strategy evaluation; Paper runners are unchanged."""
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


class LiveMarketFeed(OptimizedMainBot):
    """Use the verified reference/book readers without placing any Paper orders."""
    def __init__(self, settings, path):
        settings = replace(settings, db_path=Path(path)/"market.sqlite3", strategy_experiments_enabled=False,
                           live_trading_runtime_enabled=False, managed_live_enabled=False,
                           market_scout_enabled=False, llm_super_agent_enabled=False)
        store = TradeStore(Path(":memory:"), 1)
        super().__init__(settings, store)
        self.paper_trading_paused = True
        self.market_http=httpx.Client(timeout=httpx.Timeout(6,connect=3),follow_redirects=False)
        self.polymarket._get_json=self._market_json

    def _market_json(self, url, params=None):
        response=self.market_http.get(url,params=params)
        if response.status_code==404:return {}
        response.raise_for_status()
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
        return len(self.store.positions(self.strategy_id,open_only=True))

    def trades_for_round(self, round_id):
        return [p for p in self.store.positions(self.strategy_id) if p["round_id"]==round_id]


class LiveSignals:
    def __init__(self, settings, store, feed, cash, external_prices):
        self.settings=settings;self.store=store;self.feed=feed;self.cash=cash;self.external_prices=external_prices
        self.views={key:LiveRiskView(store,key,cash) for key in ("MAIN","MAIN_OPTIMIZED","VALUE_CONFIRM_V1","MAIN_TP_SL")}
        self.value_settings={"stake_dollars":2.0}
        self.value=ValueConfirmGuard(self.views["VALUE_CONFIRM_V1"],settings.paper_taker_fee_rate,
                                    budget_calculator=lambda account,daily:min(self.value_settings["stake_dollars"],account["cash_balance"]))
        self.basis={key:deque(maxlen=180) for key in ("okx","binance")}
        self.last_basis={}

    def evaluate(self, strategy_id, config, market, reference, quotes, now, info):
        def blocked(reason):
            return Signal("BTC","NO_TRADE",0,0,0,reason),None
        if market is None or market.target_price<=0:
            return blocked("等待本期官方参考价")
        if not reference.get("ready") or not reference.get("protocol_verified"):
            return blocked("等待同源行情和结算基准校验")
        spot=reference.get("latest",{}).get(SPOT,{})
        price_value=spot.get("price");stamp=spot.get("source_ms")
        if not isinstance(price_value,(int,float)) or not math.isfinite(price_value) or not isinstance(stamp,(int,float)) or not 0<=now*1000-stamp<=4000:
            return blocked("Chainlink 参考价未就绪")
        settings=replace(self.settings,stake_dollars=config["stake_dollars"],max_open_trades=config["max_open_trades"],
                         max_daily_loss=config["max_daily_loss"] or 1e100,paper_taker_fee_rate=info["fee_rate"])
        if strategy_id=="MAIN_OPTIMIZED":
            context=SimpleNamespace(settings=replace(settings,paper_taker_fee_rate=0),references=self.feed.references,
                                    paper_trading_paused=False,store=self.views[strategy_id])
            signal,features=OptimizedMainBot.evaluate(context,market,reference,quotes,now)
            limit=features.get("limit_price")
            if signal.side in ("Up","Down") and limit is not None:
                edge=signal.confidence-limit-fee_amount(1,limit,info["fee_rate"],info["fee_exponent"])
                if edge<max(.02,settings.min_edge):return blocked("按官方费用曲线计算后的模型优势不足")
                signal=replace(signal,reason=f"主账户优化版：实时 TWAP 模型，官方费用后优势 {edge:.4f}")
            return signal,limit
        # Override both the stale-price and wrong-target paths in live inputs only.
        price={"chainlink":price_value,"chainlink_updated_ms":stamp,"source":"PolyBolt:Chainlink"}
        try:
            external=self.external_prices()
        except Exception:
            external={}
        for source,key in (("okx","okx"),("binance","binance_market")):
            value=external.get(key);updated=external.get(key+"_updated_ms")
            if not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0 or not isinstance(updated,(int,float)) or not 0<=now*1000-updated<=4500:
                continue
            if self.last_basis.get(source)!=updated:
                self.basis[source].append((updated,(value/price_value-1)*10000));self.last_basis[source]=updated
            self.basis[source]=deque([(t,p) for t,p in self.basis[source] if t>=now*1000-90000],maxlen=180)
            price.update({key:value,key+"_updated_ms":updated,source+"_basis_samples":len(self.basis[source]),
                          source+"_basis_median_bps":statistics.median(p for _,p in self.basis[source])})
        data=replace(input_from_snapshot(market,{"price":price,"quotes":quotes}),now_ts=now)
        signal=RealBtcFiveMinuteStrategy(settings).signal(data)
        if signal.side in ("Up","Down"):
            quote=quotes.get(signal.side,{})
            token=market.up_token if signal.side=="Up" else market.down_token
            source=quote.get("source_ms");received=quote.get("received_ms")
            if str(quote.get("token_id"))!=token or not all(isinstance(v,(int,float)) for v in (source,received)) or not 0<=now*1000-source<=3000 or not 0<=now*1000-received<=2500:
                return blocked("本方向真实盘口过期或不匹配")
            bid=quote.get("best_bid");ask=quote.get("best_ask")
            if not isinstance(bid,(int,float)) or not isinstance(ask,(int,float)) or not 0<bid<=ask<1:
                return blocked("本方向盘口缺失或交叉")
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
