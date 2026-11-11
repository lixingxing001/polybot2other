"""Independent Paper account: correct TWAP contract inputs, bounded execution, auditable signals."""
from __future__ import annotations

from dataclasses import replace
import json
import math
from pathlib import Path
import threading
import time

from .bot import PaperTradingBot
from .clob_ws import ClobMarketOrderBook, CLOB_MARKET_WS_URL, _StdlibWebSocket
from .models import MarketRound, Signal
from .polymarket import PolymarketClient, _first_event_market, _jsonish_list, _target_from_market
from .storage import TradeStore
from .optimized_reference import ReferenceCache, PolyBoltReferenceFeed, SPOT, TWAP

VARIANT_ID = "MAIN_OPTIMIZED"
POLICY_VERSION = "main-optimized-20261027-v1"


def positive_price(value):
    if isinstance(value, bool):
        return None
    try:
        price=float(value)
    except (TypeError,ValueError,OverflowError):
        return None
    return price if math.isfinite(price) and price>0 else None


def trusted_book_update(book, message, source_ms, received_ms, initialized, last_sources):
    """Only complete-book snapshots and ordered level deltas can supply execution depth."""
    kind=message.get("event_type")
    if kind=="book":
        asset=str(message.get("asset_id") or "")
        if asset not in book.token_to_side or source_ms<last_sources.get(asset,0):
            return {}
        changed=book.apply_payload(message,received_ms/1000)
        initialized.add(asset);last_sources[asset]=source_ms
        return changed
    if kind!="price_change":
        return {}
    changes=[row for row in message.get("price_changes",[]) if str(row.get("asset_id")) in book.token_to_side]
    if any(str(row["asset_id"]) not in initialized for row in changes):
        raise ValueError("INITIAL_BOOK_MISSING")
    changes=[row for row in changes if source_ms>=last_sources.get(str(row["asset_id"]),0)]
    if not changes:
        return {}
    changed=book.apply_payload({**message,"price_changes":changes},received_ms/1000)
    for row in changes:last_sources[str(row["asset_id"])]=source_ms
    return changed


class OptimizedTradeStore(TradeStore):
    """Keep legacy ledger math unchanged; fix peak-to-trough reporting in this store only."""
    def __init__(self, path, initial_balance):
        super().__init__(path, initial_balance)
        self._curve_last_id = 0
        self._equity_peak = float(self.account()["initial_balance"])
        self._max_drawdown = 0.0
        self._max_drawdown_pct = 0.0

    def metrics(self):
        with self._lock:
            result = super().metrics()
            for row in self.conn.execute("SELECT id,total_equity FROM equity_curve WHERE id>? ORDER BY id", (self._curve_last_id,)):
                self._curve_last_id = row["id"]
                self._equity_peak = max(self._equity_peak, float(row["total_equity"]))
                drop = self._equity_peak-float(row["total_equity"])
                self._max_drawdown = max(self._max_drawdown, drop)
                self._max_drawdown_pct = max(self._max_drawdown_pct, drop/self._equity_peak*100 if self._equity_peak else 0)
            result.update(max_drawdown=round(self._max_drawdown,6), max_drawdown_pct=round(self._max_drawdown_pct,4))
            return result


class StrictTwapMarketClient(PolymarketClient):
    def __init__(self, settings, references):
        super().__init__(settings.gamma_url, settings.clob_url, 8)
        self.references = references
        self.market_details = {}

    def _target_from_polymarket_api(self, slug):
        return None

    def _target_from_polymarket_page(self, slug):
        return None

    @staticmethod
    def twap_contract(event):
        return "btc-usd-twap-60s" in str(event.get("resolutionSource") or "").lower()

    def read_market(self, start):
        slug = "btc-updown-5m-"+str(int(start))
        event = self._get_event_by_slug(slug)
        if not event or not self.twap_contract(event):
            return None
        raw = _first_event_market(event)
        market = self._parse_market(raw)
        if market is None or market.round_id != slug:
            return None
        official = positive_price(_target_from_market(raw))
        observed = self.references.opening(start)
        if official and observed is not None and abs(official-observed) > .01:
            self.references.verify(slug, start, official)
            raise ValueError("TWAP_OPENING_MISMATCH")
        target = official or observed or 0
        self.market_details[slug] = {"resolution_source":event.get("resolutionSource"),
                                    "target_source":"Gamma:priceToBeat" if official else "PolyBolt:TWAP60@window_start" if observed else "waiting",
                                    "official_target":official, "observed_target":observed}
        return replace(market, target_price=target)

    def get_resolution(self, slug):
        event = self._get_event_by_slug(slug)
        if not event or not self.twap_contract(event):
            return None
        raw = _first_event_market(event)
        if not raw or not raw.get("closed"):
            return None
        outcomes = _jsonish_list(raw.get("outcomes"))
        prices = _jsonish_list(raw.get("outcomePrices"))
        if len(outcomes) != len(prices):
            return None
        winners = [side for side,p in zip(outcomes,prices) if float(p) >= .999]
        metadata = event.get("eventMetadata") or {}
        target = positive_price(metadata.get("priceToBeat"))
        if len(winners)!=1 or winners[0] not in ("Up","Down") or target is None:
            return None
        return {"outcome":winners[0], "target_price":target, "final_price":metadata.get("finalPrice")}


def forecast_twap(references, market, current):
    """Zero-drift spot model for the contract's final 60-second average; score is uncalibrated."""
    spot = current[SPOT]
    observed = spot["source_ms"]/1000
    horizon = market.ends_at-observed
    points = references.points(SPOT, spot["source_ms"]-120000, spot["source_ms"])
    if len(points)<40 or points[-1][0]-points[0][0]<60000:
        return None
    variance = elapsed = 0.0
    for (t0,p0),(t1,p1) in zip(points,points[1:]):
        seconds=(t1-t0)/1000
        if seconds<=0 or seconds>4:
            continue
        variance += (math.log(p1/p0)*10000)**2
        elapsed += seconds
    if elapsed<60:
        return None
    sigma = max(.5, math.sqrt(variance/elapsed))
    if horizon>=60:
        mean = spot["price"]
        variance_time = horizon-40
    elif horizon>0:
        begin_ms=(market.ends_at-60)*1000
        known = references.points(SPOT, begin_ms-3000, spot["source_ms"])
        previous = next(((t,p) for t,p in reversed(known) if t<=begin_ms),None)
        if previous is None:
            return None
        integral=0.0;last_t=begin_ms;last_p=previous[1]
        for stamp,price in known:
            if stamp<=begin_ms:
                continue
            if stamp-last_t>4000:
                return None
            integral += last_p*(stamp-last_t)/1000
            last_t,last_p=stamp,price
        integral += last_p*max(0,spot["source_ms"]-last_t)/1000
        mean = (integral+horizon*spot["price"])/60
        variance_time = horizon**3/(3*60**2)
    else:
        return None
    std = spot["price"]*sigma*math.sqrt(max(variance_time,.01))/10000
    z = (mean-market.target_price)/max(std,1e-9)
    raw_up=.5*(1+math.erf(z/math.sqrt(2)))
    # Model uncertainty shrinkage is declared policy, not a claim of calibrated win probability.
    up_score=.5+.8*(raw_up-.5)
    return {"projected_final_twap":mean,"up_score":up_score,"raw_up_score":raw_up,
            "sigma_bps_per_sqrt_second":sigma,"remaining_sigma_dollars":std,
            "history_count":len(points),"history_seconds":elapsed,"forecast_source_ms":spot["source_ms"]}


class OptimizedMainBot(PaperTradingBot):
    independent_runtime = True

    def __init__(self, settings, store):
        super().__init__(settings, store)
        self.references=ReferenceCache(settings.db_path.with_suffix(".references.sqlite3"))
        self.polymarket=StrictTwapMarketClient(settings,self.references)
        self.reference_feed=PolyBoltReferenceFeed(self.references)
        self._reference_thread=None
        self._book_thread=None
        self._book_ws=None
        self.book_state="waiting"
        self._decision_lock=threading.Lock()
        self._lifecycle_lock=threading.RLock()
        self._resume_thread=None
        self._desired_running=False
        self._last_market_fetch=0.0
        self._next_resolution_check=0.0
        self._validated_rounds=set()
        self._last_audit_key=None
        self._last_audit_at=0.0
        self.last_decision={}
        self.last_signal={"side":"NO_TRADE","reason":"主账户优化版等待独立行情启动"}
        self.journal=settings.db_path.with_suffix(".decisions.jsonl")

    def start(self):
        with self._lifecycle_lock:
            self._desired_running=True
            if self._resume_thread and self._resume_thread.is_alive():
                return
            workers=(self._thread,self._book_thread,self._reference_thread)
            if any(worker and worker.is_alive() for worker in workers):
                if not self._stop.is_set():
                    return
                self._resume_thread=threading.Thread(target=self._resume_after_pause,args=(workers,),daemon=True)
                self._resume_thread.start()
                return
            self._start_workers()

    def _resume_after_pause(self, workers):
        for worker in workers:
            if worker and worker is not threading.current_thread():
                worker.join()
        with self._lifecycle_lock:
            self._resume_thread=None
            if self._desired_running:
                self._start_workers()

    def _start_workers(self):
        self._stop.clear()
        self.btc_runtime_paused=False
        if self._shared_inputs_required:
            self._reference_thread=None;self._book_thread=None
            self._thread=threading.Thread(target=self._optimized_loop,name="optimized-shared-paper",daemon=True)
            self._thread.start();return
        self.references.reset_connection()
        with self._lock:self.paper_quotes={};self.execution_quotes={};self.latest_quotes={}
        self._reference_thread=threading.Thread(target=self.reference_feed.run,args=(self._stop,),name="optimized-polybolt",daemon=True)
        self._book_thread=threading.Thread(target=self._book_loop,name="optimized-clob",daemon=True)
        self._thread=threading.Thread(target=self._optimized_loop,name="optimized-paper",daemon=True)
        self._reference_thread.start();self._book_thread.start();self._thread.start()

    def request_pause(self):
        with self._lifecycle_lock:
            self._desired_running=False
            self.btc_runtime_paused=True
            self._stop.set()
        if not self._shared_inputs_required:
            self.references.reset_connection("paused")
            self.reference_feed.close()
        if self._book_ws:
            try:self._book_ws.close()
            except OSError:pass

    def stop(self):
        self.request_pause()
        for worker in (self._thread,self._book_thread,self._reference_thread):
            if worker and worker is not threading.current_thread():
                worker.join(timeout=3)

    def _optimized_loop(self):
        while not self._stop.is_set():
            try:
                self.optimized_tick()
                self.last_error=None
            except Exception as exc:
                self.last_error=type(exc).__name__+": "+str(exc)[:180]
                self._signal("ERROR","独立行情或计算异常，等待恢复")
            self.last_tick_at=time.time()
            self._stop.wait(self.settings.tick_seconds)

    def _market(self, now):
        if self._shared_inputs_required:
            frame=self._sync_shared_market_inputs()
            self.book_state="shared-live"
            return frame.market if frame else None
        start=int(now)//300*300
        with self._lock:
            market=self.current_market
        if market and market.started_at==start:
            if market.target_price<=0:
                target=self.references.opening(start)
                if target:
                    market=replace(market,target_price=target)
                    self.polymarket.market_details.setdefault(market.round_id,{}).update(
                        target_source="PolyBolt:TWAP60@window_start", observed_target=target)
                    with self._lock:self.current_market=market
            return market
        if now-self._last_market_fetch<3:
            return None
        self._last_market_fetch=now
        market=self.polymarket.read_market(start)
        if market:
            with self._lock:
                self.current_market=market;self.paper_quotes={};self.execution_quotes={};self.latest_quotes={}
            self.store.upsert_round(market)
        return market

    def _resolve(self, now):
        if now<self._next_resolution_check:
            return
        self._next_resolution_check=now+15
        due={r["round_id"] for r in self.store.open_trades() if r["ends_at"]<=now}
        boundary=int(now)//300*300
        for start in set((boundary-300,boundary-600,*self.references.completed_openings(now))):
            start=int(start)
            slug="btc-updown-5m-"+str(start)
            if slug not in self._validated_rounds and self.references.opening(start) is not None:
                due.add(slug)
        for slug in sorted(due):
            resolution=self.polymarket.get_resolution(slug)
            if not resolution:
                continue
            start=int(slug.rsplit("-",1)[1])
            verified=self.references.verify(slug,start,resolution["target_price"])
            if verified is not None:
                self._validated_rounds.add(slug)
            self.store.settle_round_outcome(slug,resolution["outcome"],now,
                                           final_price=resolution["final_price"],
                                           target_price=resolution["target_price"],
                                           settlement_source="polymarket_official")

    def optimized_tick(self):
        if not self._decision_lock.acquire(blocking=False):
            return
        try:
            if self.paper_execution is not None:
                self._resolve(time.time())
                self.paper_execution.decide()
                self.store.record_equity()
                return
            now=time.time()
            market=self._market(now)
            self._resolve(time.time())
            if self._stop.is_set():
                return
            frame=self._sync_shared_market_inputs() if self._shared_inputs_required else None
            if self._shared_inputs_required:
                market=frame.market if frame else None
            if market is None:
                self._signal("MARKET_WAIT","等待当前五分钟市场")
                return
            self.store.upsert_round(market)
            if frame:
                if frame.error:
                    self._signal("SHARED_DATA",frame.error);return
                reference=frame.reference;quotes=frame.quotes;now=frame.observed_at
            else:
                reference=self.references.snapshot()
                with self._lock:quotes={k:dict(v) for k,v in self.paper_quotes.items()}
                now=time.time()
            signal,features=self.evaluate(market,reference,quotes,now)
            with self._lock:
                self.last_signal={"symbol":"BTC","side":signal.side,"confidence":signal.confidence,
                                  "entry_price":signal.entry_price,"move_bps":signal.move_bps,"reason":signal.reason}
                latest=reference["latest"]
                self.paper_price={"chainlink":latest.get(TWAP,{}).get("price"),
                                  "chainlink_updated_ms":latest.get(TWAP,{}).get("source_ms"),
                                  "twap":latest.get(TWAP,{}).get("price"),"spot":latest.get(SPOT,{}).get("price"),
                                  "source":"PolyBolt:Chainlink TWAP60"}
                self.execution_price=dict(self.paper_price);self.latest_price=dict(self.paper_price)
            key=(market.round_id,features.get("code"),signal.side)
            if key!=self._last_audit_key or time.time()-self._last_audit_at>=5:
                self._audit({"event":"decision",**features})
                self._last_audit_key=key;self._last_audit_at=time.time()
            self.last_decision=features
            if signal.side in ("Up","Down") and not self._stop.is_set():
                trade_ids=self._maybe_place_trade(market,signal,quotes)
                self._audit({"event":"execution","round_id":market.round_id,"trade_ids":trade_ids,
                             "signal":dict(self.last_signal),"execution_mode":"PAPER"})
            self.store.record_equity()
        finally:
            self._decision_lock.release()

    def evaluate(self, market, reference, quotes, now):
        features={"round_id":market.round_id,"target_price":market.target_price,
                  "time_left_seconds":market.ends_at-now,"reference":reference}
        def block(code,message):
            features["code"]=code
            return Signal("BTC","NO_TRADE",0,0,0,"主账户优化版 ["+code+"] "+message),features
        if self.paper_trading_paused:
            return block("PAPER_PAUSED","模拟交易已暂停")
        if not reference.get("ready"):
            return block("REFERENCE_STALE","等待同源现价和 TWAP 的新鲜推送")
        if not reference.get("protocol_verified"):
            return block("REFERENCE_VALIDATION","等待首个 TWAP 开盘值与官方结果核对")
        if market.target_price<=0:
            return block("OPENING_WAIT","缺少本期精确 TWAP 开盘基准")
        left=market.ends_at-now
        if not self.settings.min_time_left_seconds<=left<=self.settings.max_time_left_seconds:
            return block("TIME_WINDOW","当前不在入场时间段")
        prediction=forecast_twap(self.references,market,reference["latest"])
        if prediction is None:
            return block("WARMUP","等待足够连续的同源波动样本")
        features.update(prediction)
        side="Up" if prediction["up_score"]>=.5 else "Down"
        score=prediction["up_score"] if side=="Up" else 1-prediction["up_score"]
        move=(prediction["projected_final_twap"]-market.target_price)/market.target_price*10000
        if abs(move)<self.settings.min_price_distance_bps or score<self.settings.min_confidence:
            return block("WEAK_SIGNAL","预测结算位置或模型分数不足")
        quote=quotes.get(side) or {}
        features.update(candidate_side=side,selected_bid=quote.get("best_bid"),selected_ask=quote.get("best_ask"))
        token=market.up_token if side=="Up" else market.down_token
        if str(quote.get("token_id"))!=token:
            return block("QUOTE_MARKET","等待本期本方向盘口")
        source=quote.get("source_ms");received=quote.get("received_ms")
        features.update(quote_source_ms=source,quote_received_ms=received,
                        quote_source_age_ms=now*1000-source if isinstance(source,(int,float)) else None,
                        quote_receive_age_ms=now*1000-received if isinstance(received,(int,float)) else None)
        if not isinstance(source,(int,float)) or not isinstance(received,(int,float)) or not -1000<=now*1000-source<=3000 or not 0<=now*1000-received<=2500:
            return block("QUOTE_STALE","本方向盘口过期")
        ask=quote.get("best_ask");bid=quote.get("best_bid")
        if not all(isinstance(v,(int,float)) and math.isfinite(v) for v in (ask,bid)) or not 0<bid<=ask<1:
            return block("BOOK_INVALID","盘口缺失或交叉")
        features.update(side=side,score=score,ask=ask,bid=bid,quote_source_ms=source,quote_received_ms=received)
        if ask>self.settings.max_entry_price or ask-bid>min(.03,self.settings.max_spread):
            return block("PRICE_SPREAD","买价或价差超过范围")
        ask_levels=[float(row["price"]) for row in quote.get("asks",[]) if float(row.get("size",0))>0]
        if not ask_levels or abs(min(ask_levels)-ask)>.000001:
            return block("DEPTH_OUT_OF_SYNC","卖一与完整盘口深度不一致，等待同步")
        if now-market.started_at<60 and .5<=ask<.6:
            return block("EARLY_MID_PRICE","首分钟中等买价风险过滤")
        limit=min(self.settings.max_entry_price,ask+.01)
        cost=limit+self.settings.paper_taker_fee_rate*limit*(1-limit)
        features.update(limit_price=limit,fee_adjusted_edge=score-cost)
        if score-cost<max(.02,self.settings.min_edge):
            return block("COST_EDGE","费用和滑点后的模型优势不足")
        if self.store.daily_realized_pnl()<=-abs(self.settings.max_daily_loss):
            return block("DAILY_STOP","达到滚动 24 小时亏损限制")
        if self.store.open_trade_count("BTC")>=self.settings.max_open_trades:
            return block("POSITION_LIMIT","已有持仓达到上限")
        account=self.store.account();stake=min(self.settings.stake_dollars,account["cash_balance"])
        if stake<.1:
            return block("CASH","可用模拟资金不足")
        depth=sum(float(level["price"])*float(level["size"]) for level in quote.get("asks",[])
                  if 0<float(level["price"])<=limit and float(level["size"])>0)
        if depth<stake:
            return block("DEPTH","限价范围内深度不足")
        features.update(code="ALLOW",stake=stake)
        return Signal("BTC",side,round(score,6),ask,move,
                      "主账户优化版 [ALLOW] 同源 TWAP，滚动波动分数 "+format(score,".3f")+
                      "，净费用优势 "+format(score-cost,".3f")),features

    def _entry_limit_price(self, signal):
        return min(self.settings.max_entry_price,signal.entry_price+.01)

    def _signal(self,code,message):
        with self._lock:self.last_signal={"side":"NO_TRADE","reason":"主账户优化版 ["+code+"] "+message}
        key=("state",code)
        if key!=self._last_audit_key or time.time()-self._last_audit_at>=10:
            self._audit({"event":"state","code":code,"reason":message})
            self._last_audit_key=key;self._last_audit_at=time.time()

    def _audit(self,record):
        row={"policy_version":POLICY_VERSION,"observed_at":time.time(),**record}
        with self.journal.open("a",encoding="utf-8") as stream:
            stream.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+"\n")

    def diagnostics(self):
        with self._lock:
            market=self.current_market
            quotes={side:{key:row.get(key) for key in ("best_bid","best_ask","source_ms","received_ms")} for side,row in self.paper_quotes.items()}
        return {"policy_version":POLICY_VERSION,"running":bool(self._thread and self._thread.is_alive()),
                "market":market.round_id if market else None,"target_price":market.target_price if market else None,
                "target_source":self.polymarket.market_details.get(market.round_id,{}) if market else {},
                "reference":self.references.snapshot(),"last_decision":dict(self.last_decision),
                "last_error":self.last_error,"last_tick_at":self.last_tick_at,"book_state":self.book_state,
                "quote_summary":quotes,"journal_path":str(self.journal)}

    def _book_loop(self):
        backoff=.5
        while not self._stop.is_set():
            with self._lock:market=self.current_market
            if market is None:
                self._stop.wait(.25);continue
            try:
                self.book_state="connecting"
                self._book_ws=_StdlibWebSocket.connect(CLOB_MARKET_WS_URL,8)
                self._book_ws.send_text(json.dumps({"assets_ids":[market.up_token,market.down_token],"type":"market","custom_feature_enabled":True}))
                book=ClobMarketOrderBook.for_market(market);last_ping=time.monotonic();last_book=time.monotonic()
                initialized=set();last_sources={}
                while not self._stop.is_set():
                    if time.monotonic()-last_book>12:
                        raise ValueError("BOOK_STREAM_STALLED")
                    with self._lock:current=self.current_market
                    if current is None or current.round_id!=market.round_id:
                        break
                    if time.monotonic()-last_ping>=10:
                        self._book_ws.send_text("PING");last_ping=time.monotonic()
                    text=self._book_ws.read_text(1)
                    if not text or text in ("PING","PONG"):
                        continue
                    payload=json.loads(text)
                    for message in payload if isinstance(payload,list) else [payload]:
                        if not isinstance(message,dict):
                            continue
                        try:source_ms=int(message["timestamp"])
                        except (KeyError,TypeError,ValueError):continue
                        received_ms=time.time()*1000
                        if source_ms>received_ms+1000:
                            continue
                        changed=trusted_book_update(book,message,source_ms,received_ms,initialized,last_sources)
                        with self._lock:
                            if self.current_market.round_id!=market.round_id:
                                continue
                            for side,quote in changed.items():
                                prior=self.paper_quotes.get(side) or {}
                                if prior.get("source_ms",0)>source_ms:
                                    continue
                                quote.update(source_ms=source_ms,received_ms=received_ms,updated_at_ms=source_ms)
                                self.paper_quotes[side]=dict(quote)
                                self.execution_quotes[side]=dict(quote)
                                self.latest_quotes[side]=dict(quote)
                        if changed and -1000<=received_ms-source_ms<=3000:
                            backoff=.5;last_book=time.monotonic();self.book_state="streaming"
            except Exception:
                self.book_state="reconnecting"
                with self._lock:self.paper_quotes={};self.execution_quotes={};self.latest_quotes={}
            finally:
                if self._book_ws:
                    try:self._book_ws.close()
                    except OSError:pass
                self._book_ws=None
            if not self._stop.is_set():
                self._stop.wait(backoff);backoff=min(10,backoff*2)
