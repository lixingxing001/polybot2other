"""Five independent live strategies sharing one real wallet and one order coordinator."""
from __future__ import annotations

from dataclasses import asdict
import math
from pathlib import Path
import threading
import time
import secrets

from .live import LiveProcessLock
from .models import MarketRound
from .managed_live_store import ManagedLiveStore, STRATEGIES, ACTIVE_ORDER_STATES
from .managed_live_client import ManagedExchange, OrderConfirmationPending, OrderSubmissionUncertain, fee_amount, maximum_fee_ratio
from .managed_live_signals import LiveMarketFeed, LiveSignals
from .managed_live_recovery import (no_activity_candidate, FINALIZE_AFTER_END_SECONDS, LATE_AUDIT_FAULT,
                                   wallet_only_audit_fault, direct_order_evidence)
from .adaptive_model import AdaptiveModel

QUARANTINE_AFTER_SECONDS=60
QUARANTINE_CHECK_SECONDS=15


def sell_estimate(book, shares, info, limit=0):
    remaining=shares;gross=fee=0.
    for level in sorted(book.get("bids",[]),key=lambda x:-float(x["price"])):
        price=float(level["price"]);quantity=min(remaining,float(level["size"]))
        if price<limit or quantity<=0:continue
        gross+=quantity*price;fee+=fee_amount(quantity,price,info["fee_rate"],info["fee_exponent"])
        remaining-=quantity
        if remaining<.000001:break
    return {"shares":round(shares-remaining,6),"gross":round(gross,6),"fee":round(fee,6),
            "net":round(gross-fee,6),"full":remaining<.000001}


class ManagedLiveAccounts:
    def __init__(self, settings, owner=None, *, exchange=None, feed=None, store=None):
        self.settings=settings;self.owner=owner
        self.path=Path(settings.managed_live_dir)
        self.store=store or ManagedLiveStore(self.path/"accounts.sqlite3")
        self.exchange=exchange or ManagedExchange(settings)
        self.feed=feed or LiveMarketFeed(settings,self.path,book_reader=self.exchange.rest_book)
        self._lock=threading.RLock();self._order_lock=threading.RLock();self._stop=threading.Event()
        self._reconcile_lock=threading.Lock();self._inflight_submissions=set()
        self._threads=[];self._market_paused=False
        self.wallet={"balance":None,"total_equity":None,"ready":False,"error":"正在连接真实钱包"}
        self.geo={"ready":False,"blocked":None}
        self.open_orders=[];self.open_orders_at=0.;self._info={}
        self._valuation_books=self.store.valuation_books();self._valuation_retry_after={}
        self.last_signal={key:{"side":"NO_TRADE","reason":"实盘开仓未启用"} for key in STRATEGIES}
        self.last_error=None;self._retry_after={};self._exit_retry_after={};self._fault=None
        self._last_tick_at=0.;self._tick_wait="等待实盘策略首次检查";self._execution_statuses={}
        self._reconcile_retry_after={}
        self._wallet_wake=threading.Event();self._refresh_after=0.
        self.control_token=secrets.token_urlsafe(32)
        self.process_lock=LiveProcessLock(self.path/"wallet-orders.lock")
        self.adaptive_model=None;self.adaptive_model_error=None
        try:
            experiments_dir=Path(settings.strategy_experiments_db_dir)
            self.adaptive_model=AdaptiveModel(
                experiments_dir/"main_adaptive.model.sqlite3",
                experiments_dir/"main_optimized.sqlite3",
                experiments_dir/"main_adaptive.sqlite3",
            )
        except Exception as exc:
            self.adaptive_model_error=f"{type(exc).__name__}: {str(exc)[:120]}"
            self.store.event("adaptive_model_unavailable",f"主账户自适应成长版模型未就绪：{self.adaptive_model_error}")
        self.signals=LiveSignals(settings,self.store,self.feed,self.available_cash,self._external_prices,
                                 adaptive_model=self.adaptive_model,
                                 adaptive_model_error=self.adaptive_model_error)
        try:
            self.store.bind_wallet(self.exchange.identity)
        except Exception:
            self._fault="钱包身份无法与本地实盘账本核对"
        # A PREPARED row was durably created but never entered the submission phase.
        for order in self.store.active_orders():
            if order["state"]=="PREPARED":self.store.set_order_state(order["id"],"CANCELED","服务重启前尚未提交")
        with self.store.lock:
            if self.store.conn.execute("SELECT 1 FROM orders WHERE error='官方成交费用超出预算，需核对'").fetchone():
                self._fault="存在官方成交费用超预算记录，暂停新开仓等待核对"
            if self.store.conn.execute("SELECT 1 FROM orders WHERE ack_id IS NOT NULL AND lower(ack_id)!=lower(external_id)").fetchone():
                self._fault="官方订单编号与签名记录不一致，需要核对 SDK"
        faults=self.store.reconciliation_faults()
        if faults and not self._fault:self._fault=next(iter(faults.values()))

    def _external_prices(self):
        if self.owner is None:return {}
        with self.owner._lock:
            return dict(self.owner._auxiliary_prices if getattr(self.owner,"_shared_inputs_required",False) else self.owner.paper_price)

    def market_input_snapshot(self,advance_basis=False):
        # Only the existing live decision loop advances the auxiliary history.
        # Paper/UI reads must not change sample density or model features.
        return self.signals.market_inputs.capture(advance_basis=advance_basis)

    def start(self):
        if any(t.is_alive() for t in self._threads):return
        self._stop.clear()
        error=self.process_lock.acquire()
        if error:
            self._fault=str(error)
        if not self._market_paused:self.feed.start()
        self._threads=[threading.Thread(target=fn,name=name,daemon=True) for fn,name in
                       ((self._wallet_loop,"live-wallet"),(self._trading_loop,"live-strategies"),
                        (self._reconcile_loop,"live-reconcile"),(self._valuation_loop,"live-valuation"))]
        for worker in self._threads:worker.start()

    def stop(self):
        self._stop.set();self._wallet_wake.set();self.feed.stop()
        for worker in self._threads:
            if worker is not threading.current_thread():worker.join(timeout=3)
        if not any(t.is_alive() and t.name=="live-strategies" for t in self._threads):
            self.process_lock.release()
        if self.adaptive_model is not None:
            try:self.adaptive_model.close()
            except Exception:pass

    def set_market_paused(self, paused):
        self._market_paused=bool(paused)
        if paused:self.feed.request_pause()
        elif self._threads:self.feed.start()

    def recover_after_interruption(self,gap):
        with self._lock:
            self._refresh_after=time.time()
            self.wallet={**self.wallet,"ready":False,"stale":True,"error":"运行恢复后正在重新核对真实余额"}
            self.geo={**self.geo,"ready":False};self.open_orders_at=0
        self._wallet_wake.set()
        if hasattr(self.feed,"recover_after_interruption"):self.feed.recover_after_interruption()
        self.store.event("engine_resume",f"检测到约 {gap:.0f} 秒运行中断，重新同步行情和资金；未重复提交订单")

    def _wallet_result_fresh(self,result):
        start=result.get("read_started_at",0)
        return bool(result.get("ready") and start>=self._refresh_after and 0<=time.time()-start<=25)

    def available_cash(self):
        with self._lock:
            wallet=dict(self.wallet);external=list(self.open_orders)
        if not wallet.get("ready") or wallet.get("balance") is None:return 0.
        with self.store.lock:
            own={r[0] for r in self.store.conn.execute("SELECT external_id FROM orders")}
            own.update(r[0] for r in self.store.conn.execute("SELECT ack_id FROM orders WHERE ack_id IS NOT NULL"))
        external_reserved=sum(x["remaining"]*x["price"] for x in external if x["side"].upper()=="BUY" and x["id"] not in own)
        reserved=self.store.reserved_cash(wallet.get("read_started_at",0))
        return round(max(0,wallet["balance"]-external_reserved-reserved),6)

    def update_settings(self, payload):
        # Acknowledging OFF waits for a POST already in flight; after acknowledgement
        # no additional BUY can pass the same coordinator lock.
        with self._order_lock:
            if not isinstance(payload,dict):raise ValueError("实盘设置须为对象")
            if payload.get("enabled") is True:
                reason=self._access_block(require_wallet=True)
                if reason:raise ValueError(reason)
            self.store.update_config(payload)
        return self.snapshot()

    def _access_block(self, require_wallet=False):
        if not self.settings.live_trading_runtime_enabled:return "进程级实盘功能未启用"
        if require_wallet and self._market_paused:return "BTC 行情已暂停，暂不新开仓"
        if self._fault:return self._fault
        if not self.process_lock.locked:return "未取得本钱包的实盘执行锁"
        with self._lock:
            geo=dict(self.geo);wallet=dict(self.wallet);orders_at=self.open_orders_at
        if not geo.get("ready") or time.time()-geo.get("checked_at",0)>300:return "正在确认交易地区状态"
        if geo.get("blocked") is not False:return "Polymarket 当前地区限制禁止下单"
        if require_wallet:
            if not wallet.get("ready") or time.time()-wallet.get("checked_at",0)>25:return "真实余额未就绪，请等待连接恢复"
            if time.time()-orders_at>25:return "正在核对钱包挂单占用"
            unknown=next((o for o in self.store.active_orders() if o["state"] in ("UNKNOWN","SUBMITTING")),None)
            if unknown:return "存在结果未确认的实盘订单，暂停新开仓"+(f"：{unknown['error']}" if unknown.get("error") else "")
        return None

    def _wallet_loop(self):
        last_geo=0.
        while not self._stop.is_set():
            force=self._wallet_wake.is_set();self._wallet_wake.clear()
            try:
                fresh=self.exchange.read_wallet()
                with self._lock:
                    valid=self._wallet_result_fresh(fresh)
                    if valid:self.wallet=fresh
                    else:self.wallet={**self.wallet,"ready":False,"error":fresh.get("error") or "余额查询跨越运行中断，正在重新读取","stale":True}
                if valid:
                    self.store.record_wallet(fresh["balance"],fresh.get("total_equity"))
                if force or time.time()-last_geo>=60:
                    began=time.time()
                    geo=self.exchange.read_geo()
                    with self._lock:
                        if began<self._refresh_after or time.time()-began>25:geo={**geo,"ready":False}
                        self.geo=geo
                    last_geo=time.time() if geo.get("ready") else time.time()-55
                try:
                    began=time.time()
                    orders=self.exchange.read_open_orders()
                    with self._lock:
                        if began>=self._refresh_after and time.time()-began<=25:
                            self.open_orders=orders;self.open_orders_at=time.time()
                except Exception:
                    pass
            except Exception as exc:
                self.last_error=f"钱包连接异常（{type(exc).__name__}）"
            self._wallet_wake.wait(8)

    def _trading_loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                message=f"实盘计算暂停（{type(exc).__name__}），正在自动重试"
                if self.last_error!=message:self.store.event("engine_error",message)
                self.last_error=message
            self._stop.wait(.5)

    def _valuation_loop(self):
        while not self._stop.is_set():
            try:self.refresh_valuations()
            except Exception:
                pass  # Keep the dated last observation; display reads never affect trading.
            self._stop.wait(2)

    def refresh_valuations(self):
        """Refresh display estimates independently of execution and new-entry switches."""
        held=self.store.positions(open_only=True)
        wanted={}
        for position in held:
            token=position["token_id"]
            if token not in wanted or position["remaining_shares"]>wanted[token]["remaining_shares"]:
                wanted[token]=position
        with self._lock:
            self._valuation_books={token:book for token,book in self._valuation_books.items() if token in wanted}
        self._valuation_retry_after={token:at for token,at in self._valuation_retry_after.items() if token in wanted}
        self.store.prune_valuation_books()
        if not held or self._market_paused:return
        current,_,quotes,now=self.feed.observation()
        for token,position in wanted.items():
            if self._stop.is_set():return
            market=MarketRound(**self.store.market(position["round_id"]))
            with self._lock:previous=self._valuation_books.get(token)
            # Once a round ends, its last quote remains explicitly pending settlement.
            if market.ends_at<=now and previous:continue
            quote=quotes.get(position["outcome"],{}) if current and current.round_id==market.round_id else {}
            try:
                if (not self._fresh_quote(quote,token,now)
                        or sum(float(level["size"]) for level in quote["bids"])+.000001<position["remaining_shares"]):
                    if time.time()<self._valuation_retry_after.get(token,0):continue
                    self._valuation_retry_after[token]=time.time()+(30 if market.ends_at<=now else 5)
                    quote=self.exchange.valuation_book(token)
                info=self.exchange.market_info(market)
                if not sell_estimate(quote,position["remaining_shares"],info)["full"]:continue
                source=quote["source_ms"]/1000;received=quote["received_ms"]/1000
                if str(quote["token_id"])!=token or not 0<=time.time()-source<=30:continue
                if previous and previous["source_ms"]>=quote["source_ms"]:continue
                # Keep depth, not a per-position total: partial fills must be revalued at
                # their actual remaining quantity and basis, including depth and fees.
                observed={"round_id":market.round_id,"token_id":token,"bids":quote["bids"],
                          "source_ms":quote["source_ms"],"received_ms":quote["received_ms"],
                          "fee_rate":info["fee_rate"],"fee_exponent":info["fee_exponent"],
                          "valid_until":min(source+3,received+2.5)}
                self.store.save_valuation_book(observed)
                with self._lock:self._valuation_books[token]=observed
            except Exception:
                continue

    def _valuation_marks(self):
        with self._lock:books=dict(self._valuation_books)
        marks={}
        for position in self.store.positions(open_only=True):
            book=books.get(position["token_id"])
            if not book or book["round_id"]!=position["round_id"] or book["token_id"]!=position["token_id"]:continue
            estimate=sell_estimate(book,position["remaining_shares"],book)
            if estimate["full"]:
                marks[position["id"]]={"value":estimate["net"],"shares":position["remaining_shares"],
                                       "token_id":position["token_id"],"round_id":position["round_id"],
                                       "as_of":book["source_ms"]/1000,"valid_until":book["valid_until"]}
        return marks

    def tick(self):
        self._last_tick_at=time.time()
        if self._stop.is_set() or self._market_paused:return
        frame=self.market_input_snapshot(advance_basis=True)
        market,reference,quotes,now=frame.market,frame.reference,frame.quotes,frame.observed_at
        if market is None or market.ends_at<=now:
            self._tick_wait="等待本期实盘市场与行情";return
        self._tick_wait=None
        self.store.save_market(market)
        config=self.store.config()
        active=config["enabled"] and any(s["enabled"] for s in config["strategies"].values())
        held=self.store.positions(open_only=True)
        if not active and not held:
            return
        try:
            info=self.exchange.market_info(market);self._info[market.round_id]=info
        except Exception as exc:
            self.last_error=f"官方市场参数暂不可用（{type(exc).__name__}）";return
        for position in held:
            if position["round_id"]!=market.round_id:continue
            quote=quotes.get(position["outcome"],{})
            if not self._fresh_quote(quote,position["token_id"],now):continue
            estimate=sell_estimate(quote,position["remaining_shares"],info)
            if position["strategy_id"]=="MAIN_TP_SL" and estimate["shares"]>0:
                basis=position["remaining_cost"]*estimate["shares"]/position["remaining_shares"]
                roi=(estimate["net"]-basis)/basis*100 if basis else 0
                rule=config["strategies"]["MAIN_TP_SL"]
                reason="止盈" if roi>=rule["take_profit_pct"] else "止损" if roi<=-rule["stop_loss_pct"] else None
                if reason and self._access_block() is None and time.time()>=self._exit_retry_after.get(position["id"],0):
                    self._exit_retry_after[position["id"]]=time.time()+3
                    try:self.close_position("MAIN_TP_SL",position["id"],reason=reason)
                    except Exception as exc:
                        self.last_signal["MAIN_TP_SL"]={"side":"NO_TRADE","reason":str(exc)[:160] if isinstance(exc,ValueError) else f"退出等待重试（{type(exc).__name__}）"}
        if not active:return
        block=self._access_block(require_wallet=True)
        for key,rule in config["strategies"].items():
            if not rule["enabled"]:
                self._record_signal(key,{"side":"NO_TRADE","reason":"该策略已停止新开仓"},"off");continue
            if block:
                self._record_signal(key,{"side":"NO_TRADE","reason":block,"block_scope":"access"},"blocked");continue
            if time.time()<self._retry_after.get((key,market.round_id),0):continue
            signal,limit=self.signals.evaluate(key,rule,market,reference,quotes,now,info,market_input=frame)
            if signal.side in ("Up","Down"):
                try:
                    result=self._open(key,rule,market,signal,limit,info,config["revision"])
                    if isinstance(result,str):self._record_signal(key,{"side":"NO_TRADE","reason":result},"waiting")
                    elif isinstance(result,dict) and result["state"]=="REJECTED":
                        self._record_signal(key,{"side":"NO_TRADE","reason":result.get("error") or "官方拒绝订单，等待下一次检查"},"blocked")
                    else:self._record_signal(key,asdict(signal),"ready")
                except Exception as exc:
                    self._record_signal(key,{"side":"NO_TRADE","reason":str(exc)[:180] if isinstance(exc,ValueError) else f"实盘入场检查失败（{type(exc).__name__}）"},"blocked")
                    self._retry_after[(key,market.round_id)]=time.time()+3
            else:self._record_signal(key,asdict(signal),"waiting")
        self.last_error=None

    def _record_signal(self,key,signal,status):
        old=self.last_signal[key];now=time.time()
        changed=(old.get("state"),old.get("reason"))!=(status,signal.get("reason"))
        if changed and status=="blocked":self.store.event("entry_blocked",f"{STRATEGIES[key]} 暂停新开仓：{signal['reason']}")
        elif changed and old.get("state")=="blocked":self.store.event("entry_recovered",f"{STRATEGIES[key]} 已恢复入场检查：{signal['reason']}")
        self.last_signal[key]={**signal,"state":status,"checked_at":now,"since":now if changed else old.get("since",now)}

    @staticmethod
    def _fresh_quote(quote,token,now):
        source=quote.get("source_ms");received=quote.get("received_ms")
        bid=quote.get("best_bid");ask=quote.get("best_ask")
        if not all(isinstance(v,(int,float)) and math.isfinite(v) for v in (bid,ask)) or not 0<bid<=ask<=1:
            return False
        bids=quote.get("bids") or []
        if not bids or abs(max(float(x["price"]) for x in bids)-bid)>.000001:return False
        return (str(quote.get("token_id"))==str(token) and isinstance(source,(int,float)) and isinstance(received,(int,float))
                and 0<=now*1000-source<=3000 and 0<=now*1000-received<=2500)

    def _entry_limits(self,key,rule):
        risk=self.store.risk_usage(key)
        if risk["slots"]>=rule["max_open_trades"]:return "持仓与待确认订单已达到本策略持仓数量上限"
        daily=self.store.daily_pnl(key)
        if rule["max_daily_loss"] and daily<=-rule["max_daily_loss"]:return "达到本策略每日亏损限额"
        amount=rule["stake_dollars"]
        if rule["max_daily_loss"] and amount>rule["max_daily_loss"]+min(0,daily)-risk["loss_exposure"]+.000001:
            return "本策略剩余风控额度不足以覆盖本笔预算（含待确认风险）"
        if amount>self.available_cash()+.000001:return "真实可用余额不足本笔含费预算"
        if self.wallet.get("allowance",0)<amount:return "真实资金授权额度不足"
        return None

    def _entry_restriction(self,key,rule,market,side):
        if any(o["state"]=="QUARANTINED" for o in self.store.active_orders(round_id=market.round_id)):
            raise ValueError("本市场存在隔离订单，禁止重复开仓；其他市场按剩余额度检查")
        if self.store.has_entry(key,market.round_id,None if key=="VALUE_CONFIRM_V1" else side):return "本策略本期该方向已建仓，不重复开仓"
        if any(o["state"]!="QUARANTINED" for o in self.store.active_orders(key)):return "本策略已有订单等待官方确认"
        reason=self._entry_limits(key,rule)
        if reason:raise ValueError(reason)
        return None

    def _open(self,key,rule,market,signal,limit,info,revision):
        reason=self._entry_restriction(key,rule,market,signal.side)
        if reason:return reason
        amount=rule["stake_dollars"]
        token=market.up_token if signal.side=="Up" else market.down_token
        book=self.exchange.rest_book(token)
        if book.get("tick_size",info["tick_size"])!=info["tick_size"]:
            info=self.exchange.market_info(market,force=True)
            if book["tick_size"]!=info["tick_size"]:raise ValueError("市场价格精度正在更新，等待同步")
        tick=float(info["tick_size"])
        limit=math.floor(min(limit,1-tick)/tick+1e-9)*tick
        if limit<book["best_ask"]:raise ValueError("真实盘口已超过策略限价")
        ratio=maximum_fee_ratio(info["fee_rate"],info["fee_exponent"],tick,limit)
        budget=amount/(1+ratio)
        depth=sum(x["size"]*x["price"] for x in book["asks"] if x["price"]<=limit)
        if depth<budget or budget/limit<book["min_order_size"]:raise ValueError("真实盘口深度或最小成交份额不满足预算")
        prepared=self.exchange.prepare(market,token,"BUY",amount,limit,info)
        with self._order_lock:
            current=self.store.config()
            if not current["enabled"] or not current["strategies"][key]["enabled"] or current["revision"]!=revision:
                return "开关或金额设置已变化，等待重新检查"
            reason=self._access_block(require_wallet=True)
            if reason:raise ValueError(reason)
            reason=self._entry_restriction(key,rule,market,signal.side)
            if reason:return reason
            if self._stop.is_set() or self._market_paused or amount>self.available_cash()+.000001:return "运行状态或可用余额已变化，等待重新检查"
            current_input=self.market_input_snapshot(advance_basis=True)
            current_market,ref,current_quotes,stamp=current_input.market,current_input.reference,current_input.quotes,current_input.observed_at
            if current_market is None or current_market.round_id!=market.round_id:return "市场已切换，等待新一期信号"
            fresh,new_limit=self.signals.evaluate(key,rule,current_market,ref,current_quotes,stamp,info,market_input=current_input)
            if fresh.side!=signal.side or fresh.entry_price>limit or time.time()>=market.ends_at-self.settings.min_time_left_seconds:return "提交前复查已不满足入场条件，继续观察"
            if new_limit is None or limit>new_limit+.000001:return "提交前策略限价已变化，继续观察"
            order=self.store.prepare_order(prepared.external_id,key,market,token,signal.side,"BUY",limit,cash=amount,reason=signal.reason)
            self._retry_after[(key,market.round_id)]=time.time()+3
            self._submit(order,prepared)
            return self.store.order(order["id"])

    def _submit(self,order,prepared):
        # Publish this before SUBMITTING becomes visible to the reconciler. A GET
        # while the POST is still in flight can precede the order's existence.
        with self._lock:self._inflight_submissions.add(order["id"])
        try:
            self.store.set_order_state(order["id"],"SUBMITTING")
            try:
                response=self.exchange.submit(prepared)
                self.store.record_submission(order["id"],response)
                if response.get("external_id"):
                    self.store.record_ack(order["id"],response["external_id"])
                if response.get("identity_mismatch"):
                    self._record_reconciliation_fault(order,"官方订单编号与签名记录不一致，需要核对 SDK")
                    return
                if response.get("rejected"):
                    self.store.set_order_state(order["id"],"REJECTED",response.get("rejected_reason","官方拒绝订单，未记入成交"))
                    self.store.event("reconcile_rejected",f"{STRATEGIES[order['strategy_id']]} {order['external_id']} {response.get('rejected_reason','官方拒绝订单')}")
                else:
                    self.store.set_order_state(order["id"],"PENDING")
            except OrderSubmissionUncertain as exc:
                self.store.record_submission(order["id"],exc.details)
                self.store.set_order_state(order["id"],"UNKNOWN",f"提交结果待核对：{exc.details.get('error_message','平台响应不完整')}")
            except Exception as exc:
                status=getattr(exc,"status_code",None)
                self.store.record_submission(order["id"],{"status":"UNKNOWN","error_type":type(exc).__name__,
                                                           "http_status":status if isinstance(status,int) else None})
                self.store.set_order_state(order["id"],"UNKNOWN",f"提交结果待官方确认（{type(exc).__name__}），不会重复提交")
            self.store.event("order",f"{STRATEGIES[order['strategy_id']]} {order['action']} {order['external_id']} 已进入官方对账")
        finally:
            with self._lock:self._inflight_submissions.discard(order["id"])

    def _record_reconciliation_fault(self,order,message):
        self._fault=self._fault or message
        if self.store.record_reconciliation_fault(order["id"],message):
            self.store.event("reconcile_fault",f"{STRATEGIES[order['strategy_id']]} {order['external_id']} {message}")
        self.store.set_order_state(order["id"],"UNKNOWN",message)

    def _wait_for_confirmation(self,order,message):
        # Quarantined orders retain budget and ownership restrictions while allowing
        # independent markets to use the remaining strategy and wallet limits.
        if order["state"] not in ("UNKNOWN","QUARANTINED"):
            self.store.event("reconcile_wait",f"{STRATEGIES[order['strategy_id']]} {order['external_id']} {message}")
        self.store.set_order_state(order["id"],"QUARANTINED" if order["state"]=="QUARANTINED" else "UNKNOWN",message)

    def _maybe_quarantine(self,order):
        now=time.time()
        if order["state"] not in ("UNKNOWN","SUBMITTING","PENDING"):return
        if now-(order.get("submitted_at") or order["created_at"])<QUARANTINE_AFTER_SECONDS:return
        if self._access_block() is not None:return
        with self._lock:
            wallet=dict(self.wallet);orders_at=self.open_orders_at
            if order["id"] in self._inflight_submissions:return
        if (not wallet.get("ready") or wallet.get("balance") is None or now-wallet.get("checked_at",0)>25
                or now-orders_at>25):return
        if self.store.quarantine_order(order["id"],now):
            self.store.event("reconcile_quarantined",f"{STRATEGIES[order['strategy_id']]} {order['external_id']} 已隔离，继续只读核对；"
                             f"预留 ${order['requested_cash']:.2f}，计入持仓与亏损限额，禁止本市场重复开仓")

    def close_position(self,strategy_id,position_id,reason="手动卖出"):
        block=self._access_block()
        if block:raise ValueError(block)
        position=self.store.position(int(position_id))
        if not position or position["strategy_id"]!=strategy_id or position["remaining_shares"]<=0:
            raise ValueError("当前策略没有这笔可卖出持仓")
        if position["remaining_shares"]<.01:
            raise ValueError("剩余份额小于签名精度，保留至官方结算")
        market_data=self.store.market(position["round_id"]);market=MarketRound(**market_data)
        if time.time()>=market.ends_at:raise ValueError("市场已到期，等待官方结算")
        if self.store.active_orders(strategy_id,market.round_id):return {"pending":True}
        info=self.exchange.market_info(market)
        book=self.exchange.rest_book(position["token_id"])
        if position["remaining_shares"]+0.000001<book.get("min_order_size",0):
            raise ValueError("剩余份额低于本市场最小卖单要求，保留至官方结算")
        if book.get("tick_size",info["tick_size"])!=info["tick_size"]:
            info=self.exchange.market_info(market,force=True)
            if book["tick_size"]!=info["tick_size"]:raise ValueError("市场价格精度正在更新，等待同步")
        token=self.exchange.token_balance(position["token_id"])
        if min(token["balance"],token["allowance"])+.000001<position["remaining_shares"]:
            raise ValueError("真实代币余额或授权不足，未卖出其他策略的份额")
        tick=float(info["tick_size"])
        floor=max(tick,math.floor((book["best_bid"]-.02)/tick+1e-9)*tick)
        estimate=sell_estimate(book,position["remaining_shares"],info,floor)
        if estimate["net"]<=0:
            raise ValueError("卖出费用或买盘深度不支持正回款，等待下一次报价")
        if reason in ("止盈","止损"):
            rule=self.store.config()["strategies"][strategy_id]
            basis=position["remaining_cost"]*estimate["shares"]/position["remaining_shares"]
            roi=(estimate["net"]/basis-1)*100
            if reason=="止盈":
                if roi<rule["take_profit_pct"]:raise ValueError("最新真实盘口已不满足止盈，继续观察")
                # A take-profit SELL cannot silently turn into a loss while signing.
                target=position["remaining_cost"]/position["remaining_shares"]*(1+rule["take_profit_pct"]/100)
                lo,hi=tick,1-tick
                if hi-fee_amount(1,hi,info["fee_rate"],info["fee_exponent"])<target:
                    raise ValueError("当前价格精度无法满足止盈净回款要求")
                for _ in range(40):
                    mid=(lo+hi)/2
                    if mid-fee_amount(1,mid,info["fee_rate"],info["fee_exponent"])<target:lo=mid
                    else:hi=mid
                floor=max(floor,math.ceil(hi/tick-1e-9)*tick)
                if floor>book["best_bid"]+.000001:raise ValueError("止盈限价暂无可成交买盘")
            elif roi>-rule["stop_loss_pct"]:
                raise ValueError("最新真实盘口已恢复，未触发止损")
        prepared=self.exchange.prepare(market,position["token_id"],"SELL",position["remaining_shares"],floor,info)
        with self._order_lock:
            if self._stop.is_set() or (self._market_paused and reason!="手动卖出"):
                raise ValueError("自动交易已暂停，卖出尚未提交")
            block=self._access_block()
            if block:raise ValueError(block)
            current=self.store.position(position["id"])
            if current["remaining_shares"]!=position["remaining_shares"]:raise ValueError("持仓刚刚变化，请刷新后重试")
            if time.time()>=market.ends_at:raise ValueError("市场已到期")
            order=self.store.prepare_order(prepared.external_id,strategy_id,market,position["token_id"],position["outcome"],
                                           "SELL",floor,shares=position["remaining_shares"],position_id=position["id"],reason=reason)
            self._submit(order,prepared)
            return self.store.order(order["id"])

    def _reconcile_loop(self):
        while not self._stop.is_set():
            try:self.reconcile()
            except Exception as exc:self.last_error=f"实盘订单对账暂不可用（{type(exc).__name__}）"
            self._stop.wait(3)

    def reconcile(self):
        if not self._reconcile_lock.acquire(blocking=False):return
        try:self._reconcile_orders()
        finally:self._reconcile_lock.release()

    def _reconcile_orders(self):
        faults=self.store.reconciliation_faults()
        orders=sorted(self.store.active_orders(),key=lambda o:(o["state"]=="QUARANTINED",o["created_at"]))
        audit=self.store.finalization_audit_candidate()
        if audit:orders.append(audit)
        for pending in orders:
            with self._lock:
                if pending["id"] in self._inflight_submissions:continue
            order=self.store.order(pending["id"])
            if not order or order["state"] not in (*ACTIVE_ORDER_STATES,"EXPIRED_UNFILLED") or order["state"]=="PREPARED":continue
            reviewing_activity_fault=(faults.get(order["id"])==LATE_AUDIT_FAULT and wallet_only_audit_fault(order))
            if order["id"] in faults and not reviewing_activity_fault:continue
            if order.get("ack_id") and order["ack_id"].lower()!=order["external_id"].lower():continue
            if order["state"]=="QUARANTINED" or reviewing_activity_fault:
                if time.time()<self._reconcile_retry_after.get(order["id"],0):continue
                self._reconcile_retry_after[order["id"]]=time.time()+QUARANTINE_CHECK_SECONDS
            market=MarketRound(**self.store.market(order["round_id"]))
            try:
                info=self.exchange.market_info(market)
                query_order={**order,"state":"EXPIRED_UNFILLED"} if reviewing_activity_fault else order
                state=self.exchange.reconcile(query_order,market,info)
                checked=dict(state.get("evidence",{"order":"checked"}))
                if state["fills"]:checked["confirmed_fills"]=state["fills"]
                if self.store.recover_fak_no_match(order["id"],checked):
                    self._reconcile_retry_after.pop(order["id"],None)
                    self.store.event("reconcile_rejected",f"{STRATEGIES[order['strategy_id']]} FAK 无匹配成交，已释放 ${order['requested_cash']:.2f} 预留")
                    self._wallet_wake.set()
                    continue
                if reviewing_activity_fault or (order["state"]=="QUARANTINED" and order.get("finalized_at")):
                    recovered=self.store.recover_wallet_activity(order["id"],checked)
                    if recovered:
                        faults=self.store.reconciliation_faults()
                        if self._fault==LATE_AUDIT_FAULT:self._fault=next(iter(faults.values()),None)
                        self._reconcile_retry_after.pop(order["id"],None)
                        self.store.event("reconcile_activity_recovered",f"{STRATEGIES[order['strategy_id']]} "
                                         +self.store.order(order["id"])["error"])
                        continue
                    if reviewing_activity_fault:
                        # Never overwrite proven trade evidence with a later empty
                        # query, or turn an unavailable read into automatic recovery.
                        if direct_order_evidence(checked):self.store.record_verification(order["id"],checked)
                        continue
                self.store.record_verification(order["id"],checked)
                if order["state"]=="EXPIRED_UNFILLED":
                    evidence=state.get("evidence",{})
                    if state["terminal"] and state["no_fill"]:
                        self.store.set_order_state(order["id"],"CANCELED","后续官方订单记录确认未成交")
                    elif state["fills"] or direct_order_evidence(evidence):
                        self._record_reconciliation_fault(order,LATE_AUDIT_FAULT)
                    elif evidence.get("wallet_activity_count",0)>0:
                        self.store.set_order_state(order["id"],"QUARANTINED","到期后发现钱包活动，尚未关联到该订单；仅隔离该订单核对")
                        self.store.event("reconcile_activity_isolated",f"{STRATEGIES[order['strategy_id']]} 旧订单仅隔离核对，其他市场按剩余额度运行")
                    continue
                for fill in state["fills"]:
                    self.store.apply_confirmed_fill(order["id"],**fill)
                if self.store.order(order["id"]).get("error")=="官方成交费用超出预算，需核对":
                    self._fault="存在官方成交费用超预算记录，暂停新开仓等待核对"
                if state["terminal"]:
                    message=self.store.order(order["id"]).get("error")
                    if message!="官方成交费用超出预算，需核对":message=None
                    self.store.set_order_state(order["id"],"CANCELED" if state["no_fill"] else "FILLED",message)
                    result="确认未成交" if state["no_fill"] else "确认成交"
                    self._reconcile_retry_after.pop(order["id"],None)
                    self.store.event("reconcile_recovered" if order["state"] in ("UNKNOWN","SUBMITTING","QUARANTINED") else "reconcile_confirmed",
                                     f"{STRATEGIES[order['strategy_id']]} {order['external_id']} 官方对账已完成：{result}")
                elif state.get("pending_reason"):
                    self._wait_for_confirmation(order,state["pending_reason"])
                if not state["terminal"]:self._try_finalize_missing(self.store.order(order["id"]),market,state.get("evidence",{}))
            except OrderConfirmationPending as exc:
                if reviewing_activity_fault:continue
                if order["state"]=="EXPIRED_UNFILLED":
                    self.store.record_verification(order["id"],{"order":"incomplete","message":str(exc)});continue
                self._wait_for_confirmation(order,str(exc))
                self.store.record_verification(order["id"],{"order":"incomplete","message":str(exc)})
                self.store.record_closure_probe(order["id"],False,{})
            except ValueError as exc:
                if reviewing_activity_fault:self.store.record_verification(order["id"],{"order":"integrity_conflict","message":str(exc)[:100]})
                self._record_reconciliation_fault(order,f"实盘成交需核对：{str(exc)[:100]}")
            except Exception as exc:
                if reviewing_activity_fault:continue
                http_status=getattr(exc,"status_code",None)
                detail=f"HTTP {http_status}" if isinstance(http_status,int) and 100<=http_status<=599 else type(exc).__name__
                self.store.record_verification(order["id"],{"order":"unavailable","error":detail})
                if order["state"]=="EXPIRED_UNFILLED":continue
                self._wait_for_confirmation(order,f"官方对账暂不可用（{detail}），等待确认")
                self.store.record_closure_probe(order["id"],False,{})
            self._maybe_quarantine(self.store.order(order["id"]))
        rounds={p["round_id"] for p in self.store.positions(open_only=True)}
        for slug in rounds:
            market=self.store.market(slug)
            if market["ends_at"]>time.time():continue
            resolution=self.feed.polymarket.get_resolution(slug)
            if resolution and resolution.get("outcome") in ("Up","Down"):
                self.store.resolve(slug,resolution["outcome"])

    def _try_finalize_missing(self,order,market,evidence):
        if order["state"]!="QUARANTINED":return
        now=time.time();proof=dict(evidence)
        if now-market.ends_at>=FINALIZE_AFTER_END_SECONDS and evidence.get("wallet_activity")=="checked":
            try:
                resolution=self.feed.polymarket.get_resolution(market.round_id)
                proof.update(market_resolved=bool(resolution and resolution.get("outcome") in ("Up","Down")),
                             resolution_outcome=resolution.get("outcome") if resolution else None,market_ends_at=market.ends_at)
            except Exception as exc:proof.update(market_resolved=False,resolution_error=type(exc).__name__)
        with self._lock:wallet=dict(self.wallet);open_orders_at=self.open_orders_at
        proof["wallet_verified"]=(not self._fault and self.process_lock.locked and wallet.get("ready") is True
            and isinstance(wallet.get("balance"),(int,float)) and math.isfinite(wallet["balance"]) and wallet["balance"]>=0
            and 0<=time.time()-wallet.get("checked_at",0)<=25 and 0<=time.time()-open_orders_at<=25)
        if order["action"]=="SELL":
            position=self.store.position(order["position_id"])
            proof["owned_position_open"]=bool(position and position["remaining_shares"]>0 and position["token_id"]==order["token_id"])
            proof["owned_token_shares"]=sum(p["remaining_shares"] for p in self.store.positions(open_only=True) if p["token_id"]==order["token_id"])
        self.store.record_verification(order["id"],proof)
        if self.store.record_closure_probe(order["id"],no_activity_candidate(order,market,proof,time.time()),proof):
            self._reconcile_retry_after.pop(order["id"],None)
            self.store.event("reconcile_finalized",f"{STRATEGIES[order['strategy_id']]} {order['external_id']} 市场已结算，三次多源核对无成交，释放资金与名额；继续低频复查")

    def _fault_recovers_automatically(self):
        if self._fault!=LATE_AUDIT_FAULT:return False
        faults=self.store.reconciliation_faults()
        return bool(faults) and all(message==LATE_AUDIT_FAULT and wallet_only_audit_fault(self.store.order(ident) or {})
                                   for ident,message in faults.items())

    def _execution_status(self,row,config,market,block,now):
        key=row["variant_id"];signal=self.last_signal[key];rule=config["strategies"][key]
        state="waiting";reason=signal.get("reason") or "等待入场信号";recovery="auto"
        if signal.get("block_scope")=="access":reason="安全检查已通过，等待下一次入场检查"
        if not config["enabled"] or not rule["enabled"]:
            state="off";reason="实盘总开关已关闭" if not config["enabled"] else "该策略新开仓已关闭";recovery="manual"
        elif block:
            manual_fault=bool(self._fault) and not self._fault_recovers_automatically()
            state="blocked";reason=block;recovery="manual" if manual_fault or not self.settings.live_trading_runtime_enabled or self._market_paused else "auto"
        elif self._threads and not any(t.is_alive() and t.name=="live-strategies" for t in self._threads):
            state="blocked";reason="实盘策略线程未运行，需要检查服务";recovery="manual"
        elif self.last_error:
            state="blocked";reason=self.last_error
        elif self._last_tick_at and now-self._last_tick_at>15:
            state="blocked";reason="实盘策略检查超过 15 秒未更新，等待连接恢复"
        elif self._tick_wait:
            reason=self._tick_wait
        elif market and any(o["state"]=="QUARANTINED" for o in self.store.active_orders(round_id=market.round_id)):
            state="blocked";reason="本市场存在隔离订单，禁止重复开仓；其他市场按剩余额度检查"
        elif any(o["state"] in ACTIVE_ORDER_STATES and o["state"]!="QUARANTINED" for o in row["orders"]):
            state="confirming";reason="订单已提交，等待官方确认；不会重复提交"
        elif self._entry_limits(key,rule):
            state="blocked";reason=self._entry_limits(key,rule)
        elif signal.get("state")=="blocked" and signal.get("block_scope")!="access":
            state="blocked"
        elif signal.get("side") in ("Up","Down"):
            state="ready";reason="入场检查已通过，等待提交结果"
        labels={"off":"新开仓已关闭","blocked":"暂停新开仓","waiting":"等待入场条件","confirming":"订单确认中","ready":"正在处理入场"}
        previous=self._execution_statuses.get(key,{})
        since=previous.get("since",now) if (previous.get("state"),previous.get("reason"))==(state,reason) else now
        result={"state":state,"label":labels[state],"reason":reason,"since":since,"checked_at":self._last_tick_at or None,
                "recovery":recovery,"retry_at":self._retry_after.get((key,market.round_id),0) if market else None}
        result["quarantined_count"]=row["risk"]["quarantined_count"]
        result["quarantined_cash"]=row["risk"]["quarantined_cash"]
        self._execution_statuses[key]=result
        return result

    def snapshot(self,market_input=None):
        with self._lock:
            wallet=dict(self.wallet);geo=dict(self.geo)
        if wallet.get("checked_at") and time.time()-wallet["checked_at"]>25:
            wallet["stale"]=True
        wallet["available"]=self.available_cash() if wallet.get("ready") and not wallet.get("stale") else None
        wallet["reserved"]=round(max(0,wallet["balance"]-wallet["available"]),6) if wallet.get("available") is not None else None
        data=self.store.snapshot(self._valuation_marks())
        frame=market_input or self.market_input_snapshot()
        market,reference,quotes,observed_at=frame.market,frame.reference,frame.quotes,frame.observed_at
        block=self._access_block(require_wallet=True)
        for row in data["variants"]:
            row["last_signal"]=self.last_signal[row["variant_id"]]
            row["execution"]=self._execution_status(row,data["settings"],market,block,time.time())
        market_data={"market":asdict(market) if market else None,"observed_at":observed_at,"input_source":"managed-live-verified",
                     "snapshot_id":frame.snapshot_id,
                     "reference_ready":reference.get("ready",False),"reference_verified":reference.get("protocol_verified",False),
                     "spot":reference.get("latest",{}).get("price.crypto",{}).get("price"),
                     "twap":reference.get("latest",{}).get("price.crypto.twap",{}).get("price"),
                     "quotes":{k:{f:v.get(f) for f in ("best_bid","best_ask","source_ms","received_ms","source")} for k,v in quotes.items()},
                     "feed_health":self.feed.health_snapshot() if hasattr(self.feed,"health_snapshot") else None}
        if self.adaptive_model is not None:
            try:adaptive_model=self.adaptive_model.snapshot()
            except Exception as exc:adaptive_model={"state":"UNAVAILABLE","error":f"{type(exc).__name__}: {str(exc)[:120]}"}
        else:
            adaptive_model={"state":"UNAVAILABLE","error":self.adaptive_model_error or "模型未初始化"}
        data.update(available=True,wallet=wallet,geo=geo,market_data=market_data,runtime_enabled=self.settings.live_trading_runtime_enabled,
                    running=any(t.is_alive() for t in self._threads),error=self._fault or self.last_error,
                    entry_block_reason=block,market_paused=self._market_paused,
                    adaptive_model=adaptive_model,
                    balance_scope="钱包余额和总资产由五个实盘策略共享；策略盈亏与持仓单独统计。",
                    off_policy="关闭开关只停止新开仓，已有实盘持仓继续管理退出。",
                    fee_note="策略盈亏依据官方已确认成交和官方市场费率计算；待领取收益不是现金余额。")
        return data
