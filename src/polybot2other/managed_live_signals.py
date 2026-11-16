"""Verified shared market collector and the existing live strategy adapters."""
from dataclasses import replace
from collections import deque
from pathlib import Path
import math
import statistics
import time
import uuid
from types import SimpleNamespace
import httpx

from .models import Signal
from .adaptive_main import AdaptiveDecisionJournal, compact_adaptive_decision
from .adaptive_features import current_reference_features
from .storage import TradeStore
from .strategy import RealBtcFiveMinuteStrategy, input_from_snapshot
from .optimized_main import OptimizedMainBot
from .optimized_fast import FastMainBot, FastDecisionJournal, compact_fast_decision, assess_fast_cost
from .optimized_plus import PlusMainBot, PlusDecisionJournal, compact_plus_decision, assess_plus_cost
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
    def __init__(self, settings, store, feed, cash, external_prices, adaptive_model=None, adaptive_model_error=None, execution_mode="live"):
        self.settings=settings;self.store=store;self.feed=feed;self.cash=cash;self.external_prices=external_prices
        self.views={key:LiveRiskView(store,key,cash) for key in ("MAIN","MAIN_OPTIMIZED","MAIN_ADAPTIVE","MAIN_OPTIMIZED_FAST","MAIN_OPTIMIZED_PLUS","VALUE_CONFIRM_V1","MAIN_TP_SL")}
        self.adaptive_model=adaptive_model
        self.adaptive_model_error=adaptive_model_error
        self.last_features={}
        self.execution_mode = execution_mode
        model_path = getattr(adaptive_model, "path", None)
        journal_dir = (Path(model_path).parent if isinstance(model_path, (str, Path))
                       else Path(store.path).parent)
        self.adaptive_journal = AdaptiveDecisionJournal(
            journal_dir / ("main_adaptive." + execution_mode + "-decisions-v2.jsonl"), execution_mode)
        self.fast_journal = FastDecisionJournal(
            journal_dir / ("main_optimized_fast." + execution_mode + "-decisions-v1.jsonl"), execution_mode)
        self.plus_journal = PlusDecisionJournal(
            journal_dir / ("main_optimized_plus." + execution_mode + "-decisions-v1.jsonl"), execution_mode)
        self.value_settings={"stake_dollars":2.0}
        self.value=ValueConfirmGuard(self.views["VALUE_CONFIRM_V1"],settings.paper_taker_fee_rate,
                                    budget_calculator=lambda account,daily:min(self.value_settings["stake_dollars"],account["cash_balance"]))
        self.market_inputs=SharedMarketInputs(feed,external_prices)

    def adaptive_snapshot(self, *, market=None, limit=None, info=None):
        """Immutable evidence for exactly the current re-evaluation, bound before submit."""
        data = compact_adaptive_decision(self.last_features, mode=self.execution_mode)
        if market is not None and data.get("round_id") != market.round_id:
            raise ValueError("成长版决策快照与当前市场不一致，重新评估")
        if (not data.get("adaptive_allowed") or data.get("candidate_side") not in ("Up", "Down")
                or not isinstance(data.get("base_selected_score"), (float, int))
                or not isinstance(data.get("adaptive_score"), (float, int))):
            raise ValueError("成长版缺少可验证的入场决策快照，重新评估")
        if limit is not None and info is not None:
            cost = limit + fee_amount(1, limit, info["fee_rate"], info["fee_exponent"])
            edge = data["adaptive_score"] - cost
            if edge + .000001 < data.get("min_edge", .02):
                raise ValueError("成长版提交限价的费用后优势不足")
            data.update(submission_limit=limit, submission_all_in_cost=cost, submission_expected_edge=edge)
        return data

    def record_adaptive_state(self, reason, state, *, market=None, force=False):
        features = self.last_features if getattr(self, "last_strategy_id", None) == "MAIN_ADAPTIVE" else {}
        if market is not None and features.get("round_id") != market.round_id:
            features = {"round_id": market.round_id, "decision_at": time.time()}
        self.adaptive_journal.record({**features, "stage": state, "state": state, "reason": reason}, force=force)

    def fast_snapshot(self, *, market=None, limit=None, info=None):
        """Persist the exact final FAST assessment with the order, before submission."""
        data = compact_fast_decision(self.last_features, mode=self.execution_mode)
        if market is not None and data.get("round_id") != market.round_id:
            raise ValueError("极速版决策快照与当前市场不一致，重新评估")
        score = data.get("score")
        if (not data.get("fast_allowed") or data.get("candidate_side") not in ("Up", "Down")
                or isinstance(score, bool) or not isinstance(score, (float, int))
                or not math.isfinite(score) or not 0 < score < 1):
            raise ValueError("极速版缺少可验证的入场决策快照，重新评估")
        if limit is not None and info is not None:
            cost = limit + fee_amount(1, limit, info["fee_rate"], info["fee_exponent"])
            gate = assess_fast_cost(score, cost, min_edge=data.get("min_edge", .02))
            if not gate["allowed"]:
                raise ValueError("极速版提交限价的费用后优势或预期回报不足")
            data.update(submission_limit=limit, submission_all_in_cost=cost,
                        submission_expected_edge=gate["expected_edge"], submission_expected_roi=gate["expected_roi"])
        return data

    def record_fast_state(self, reason, state, *, market=None, force=False):
        features = self.last_features if getattr(self, "last_strategy_id", None) == "MAIN_OPTIMIZED_FAST" else {}
        if market is not None and features.get("round_id") != market.round_id:
            features = {"round_id": market.round_id, "decision_at": time.time()}
        self.fast_journal.record({**features, "stage": state, "state": state, "reason": reason}, force=force)

    def plus_snapshot(self, *, market=None, limit=None, info=None):
        """Bind the final PLUS decision and official execution cost before submit."""
        data = compact_plus_decision(self.last_features, mode=self.execution_mode)
        if market is not None and data.get("round_id") != market.round_id:
            raise ValueError("进阶版决策快照与当前市场不一致，重新评估")
        score = data.get("score")
        if (not data.get("plus_allowed") or data.get("candidate_side") not in ("Up", "Down")
                or isinstance(score, bool) or not isinstance(score, (float, int))
                or not math.isfinite(score) or not 0 < score < 1):
            raise ValueError("进阶版缺少可验证的入场决策快照，重新评估")
        if limit is not None and info is not None:
            cost = limit + fee_amount(1, limit, info["fee_rate"], info["fee_exponent"])
            gate = assess_plus_cost(score, cost, min_edge=data.get("min_edge", .02))
            if not gate["allowed"]:
                raise ValueError("进阶版提交限价的费用后优势不足")
            data.update(submission_limit=limit, submission_all_in_cost=cost,
                        submission_expected_edge=gate["expected_edge"])
        return data

    def record_plus_state(self, reason, state, *, market=None, force=False):
        features = self.last_features if getattr(self, "last_strategy_id", None) == "MAIN_OPTIMIZED_PLUS" else {}
        if market is not None and features.get("round_id") != market.round_id:
            features = {"round_id": market.round_id, "decision_at": time.time()}
        self.plus_journal.record({**features, "stage": state, "state": state, "reason": reason}, force=force)

    def evaluate(self, strategy_id, config, market, reference, quotes, now, info, *, market_input=None):
        # A blocked call must never inherit the preceding strategy/round's features.
        self.last_features={}
        self.last_strategy_id = strategy_id
        adaptive = strategy_id == "MAIN_ADAPTIVE"
        fast = strategy_id == "MAIN_OPTIMIZED_FAST"
        plus = strategy_id == "MAIN_OPTIMIZED_PLUS"
        seed = {"round_id": getattr(market, "round_id", None), "decision_at": now,
                "observed_at": now, "decision_id": uuid.uuid4().hex,
                "mode": getattr(self, "execution_mode", "live")}
        def recorded(signal, limit):
            if adaptive:
                if signal.side not in ("Up", "Down"):
                    self.last_features["adaptive_allowed"] = False
                self.last_features = {**seed, **self.last_features, "reason": signal.reason,
                                      "state": "candidate" if signal.side in ("Up", "Down") else "rejected"}
                journal = getattr(self, "adaptive_journal", None)
                if journal is not None: journal.record(self.last_features)
            if fast:
                if signal.side not in ("Up", "Down"):
                    self.last_features["fast_allowed"] = False
                self.last_features = {**seed, **self.last_features, "reason": signal.reason,
                                      "state": "candidate" if signal.side in ("Up", "Down") else "rejected"}
                journal = getattr(self, "fast_journal", None)
                if journal is not None: journal.record(self.last_features)
            if plus:
                if signal.side not in ("Up", "Down"):
                    self.last_features["plus_allowed"] = False
                self.last_features = {**seed, **self.last_features, "reason": signal.reason,
                                      "state": "candidate" if signal.side in ("Up", "Down") else "rejected"}
                journal = getattr(self, "plus_journal", None)
                if journal is not None: journal.record(self.last_features)
            return signal, limit
        def blocked(reason):
            return recorded(Signal("BTC","NO_TRADE",0,0,0,reason),None)
        frame=market_input or self.market_inputs.build(market,reference,quotes,now)
        if frame.error:return blocked(frame.error)
        market,reference,quotes,now=frame.market,frame.reference,frame.quotes,frame.observed_at
        seed.update(round_id=market.round_id, decision_at=now, observed_at=now,
                    time_left_seconds=market.ends_at-now, market_ends_at=market.ends_at, started_at=market.started_at,
                    target_price=market.target_price, snapshot_id=getattr(frame,"snapshot_id",None))
        settings=replace(self.settings,stake_dollars=config["stake_dollars"],max_open_trades=config["max_open_trades"],
                         max_daily_loss=config["max_daily_loss"] or 1e100,paper_taker_fee_rate=info["fee_rate"])
        if fast:
            context=SimpleNamespace(settings=replace(settings,paper_taker_fee_rate=0),references=self.feed.references,
                                    paper_trading_paused=False,store=self.views[strategy_id],
                                    strategy_label="主账户（优化极速赚钱版）",execution_cost_source="pending-official")
            signal,features=FastMainBot.evaluate(context,market,reference,quotes,now)
            self.last_features=dict(features)
            limit=features.get("limit_price")
            if signal.side in ("Up", "Down") and limit is not None:
                fee=fee_amount(1,limit,info["fee_rate"],info["fee_exponent"])
                score=features.get("score",signal.confidence)
                gate=assess_fast_cost(score,limit+fee,min_edge=max(.02,settings.min_edge))
                self.last_features.update(gate,score=score,base_selected_score=score,
                    candidate_side=signal.side,entry_price=signal.entry_price,
                    all_in_cost=limit+fee,fee_rate=info["fee_rate"],fee_exponent=info["fee_exponent"],
                    fee_per_share=fee,fee_source="official-market",fast_allowed=gate["allowed"])
                if not gate["allowed"]:
                    self.last_features["code"]="FAST_COST_EDGE"
                    return blocked("主账户（优化极速赚钱版） [FAST_COST_EDGE] "+gate["reason"])
                signal=replace(signal,reason="主账户（优化极速赚钱版）：官方费用后优势 "
                    +format(gate["expected_edge"],".4f")+"，预期预算回报 "+format(gate["expected_roi"],".2%"))
            return recorded(signal,limit)
        if plus:
            context=SimpleNamespace(settings=replace(settings,paper_taker_fee_rate=0),references=self.feed.references,
                                    paper_trading_paused=False,store=self.views[strategy_id],
                                    strategy_label="主账户（优化进阶版）",execution_cost_source="pending-official")
            signal,features=PlusMainBot.evaluate(context,market,reference,quotes,now)
            self.last_features=dict(features)
            limit=features.get("limit_price")
            if signal.side in ("Up", "Down") and limit is not None:
                fee=fee_amount(1,limit,info["fee_rate"],info["fee_exponent"])
                score=features.get("score",signal.confidence)
                gate=assess_plus_cost(score,limit+fee,min_edge=max(.02,settings.min_edge))
                self.last_features.update(gate,score=score,base_selected_score=score,
                    candidate_side=signal.side,entry_price=signal.entry_price,
                    all_in_cost=limit+fee,fee_rate=info["fee_rate"],fee_exponent=info["fee_exponent"],
                    fee_per_share=fee,fee_source="official-market",plus_allowed=gate["allowed"])
                if not gate["allowed"]:
                    self.last_features["code"]="PLUS_COST_EDGE"
                    return blocked("主账户（优化进阶版） [PLUS_COST_EDGE] "+gate["reason"])
                signal=replace(signal,reason="主账户（优化进阶版）：官方费用后优势 "
                    +format(gate["expected_edge"],".4f")+"，每期一次与动量检查通过")
            return recorded(signal,limit)
        if strategy_id in {"MAIN_OPTIMIZED", "MAIN_ADAPTIVE"}:
            if adaptive and self.adaptive_model is None:
                detail=self.adaptive_model_error or "模型文件不可用"
                return blocked(f"主账户自适应成长版：自适应模型未就绪（{detail}）")
            adjust_prediction = self.adaptive_model.adjust_prediction if adaptive else None
            context=SimpleNamespace(settings=replace(settings,paper_taker_fee_rate=0),references=self.feed.references,
                                    paper_trading_paused=False,
                                    store=self.views.get(strategy_id, self.views["MAIN_OPTIMIZED"]),
                                    strategy_label=("主账户自适应成长版" if adaptive else "主账户优化版"),
                                    adjust_prediction=adjust_prediction or (lambda prediction, _market, _now: prediction))
            signal,features=OptimizedMainBot.evaluate(context,market,reference,quotes,now)
            self.last_features=dict(features)
            limit=features.get("limit_price")
            if signal.side in ("Up","Down") and limit is not None:
                fee=fee_amount(1,limit,info["fee_rate"],info["fee_exponent"])
                cost=limit+fee
                if adaptive:
                    features.update(current_reference_features(self.feed.references,decision_at=now,
                        ask=features.get("ask"),bid=features.get("bid"),target_price=market.target_price))
                    assessment=self.adaptive_model.assess_entry(
                        features,market,now,entry_price=signal.entry_price,
                        all_in_cost=cost,min_edge=max(.02,settings.min_edge))
                    self.last_features.update(assessment, limit_price=limit, fee_rate=info["fee_rate"],
                                              fee_exponent=info["fee_exponent"], fee_per_share=fee,
                                              move_bps=signal.move_bps, fee_source="official-market")
                    if self.execution_mode == "paper":
                        observe = getattr(self.adaptive_model, "record_observation", None)
                        if callable(observe):
                            observe(compact_adaptive_decision({**seed, **self.last_features}, mode="paper"))
                    if not assessment["adaptive_allowed"]:
                        self.last_features["code"]="ADAPTIVE_EDGE"
                        return blocked("主账户自适应成长版 [ADAPTIVE_EDGE] "+assessment["adaptive_reason"])
                    score=assessment["adaptive_score"]
                    if (not isinstance(score,(int,float)) or not math.isfinite(score) or not 0<score<1
                            or assessment.get("candidate_side",signal.side)!=signal.side):
                        return blocked("主账户自适应成长版：模型决策无效，等待重新评估")
                    signal=replace(signal,confidence=round(score,6))
                edge=(signal.confidence-cost if adaptive else signal.confidence-limit-fee)
                if (edge+1e-9 if adaptive else edge)<max(.02,settings.min_edge):return blocked("按官方费用曲线计算后的模型优势不足")
                label = "主账户自适应成长版" if adaptive else "主账户优化版"
                signal=replace(signal,reason=f"{label}：实时 TWAP 模型，官方费用后优势 {edge:.4f}")
            return recorded(signal,limit)
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
