"""Read-only live books: complete depth, ordered deltas, bounded publication and REST repair."""
import json
import math
import threading
import time

from .buffered_ws import BufferedWebSocket
from .clob_ws import CLOB_MARKET_WS_URL


def fresh_book(quote,token,now,source_age=3000,receive_age=2500):
    values=[quote.get(k) for k in ("best_bid","best_ask","source_ms","received_ms")]
    if not all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in values):return False
    bid,ask,source,received=values
    if str(quote.get("token_id"))!=str(token) or not 0<bid<=ask<1:return False
    bids=quote.get("bids") or [];asks=quote.get("asks") or []
    if not bids or not asks:return False
    return (abs(max(float(x["price"]) for x in bids)-bid)<.000001
            and abs(min(float(x["price"]) for x in asks)-ask)<.000001
            and 0<=now*1000-source<=source_age and 0<=now*1000-received<=receive_age)


class LiveDepthBook:
    """Keep all levels internally; trimming a published view must not lose future top levels."""
    def __init__(self,market):
        self.tokens={str(market.up_token):"Up",str(market.down_token):"Down"};self.depth={};self.stamps={};self.tops={}

    @staticmethod
    def number(value):
        if isinstance(value,bool):raise ValueError("BOOK_VALUE_INVALID")
        value=float(value)
        if not math.isfinite(value):raise ValueError("BOOK_VALUE_INVALID")
        return value

    def apply(self,message,source_ms,received_ms):
        kind=message.get("event_type");changed=set()
        if kind=="book":
            token=str(message.get("asset_id") or "")
            if token not in self.tokens:return changed
            if source_ms<self.stamps.get(token,(0,0))[0]:raise ValueError("BOOK_OUT_OF_ORDER")
            sides={}
            for name in ("bids","asks"):
                levels={}
                for row in message.get(name,[]):
                    price=self.number(row["price"]);size=self.number(row["size"])
                    if 0<price<1 and size>0:levels[price]=size
                sides[name]=levels
            self.depth[token]=sides;self.tops.pop(token,None);changed.add(token)
        elif kind=="price_change":
            for row in message.get("price_changes",[]):
                token=str(row.get("asset_id") or "")
                if token not in self.tokens:continue
                if token not in self.depth:raise ValueError("INITIAL_BOOK_MISSING")
                if source_ms<self.stamps[token][0]:raise ValueError("BOOK_OUT_OF_ORDER")
                name={"BUY":"bids","SELL":"asks"}.get(str(row.get("side","")).upper())
                if not name:raise ValueError("BOOK_DELTA_INCOMPLETE")
                price=self.number(row["price"]);size=self.number(row["size"])
                if size<0:raise ValueError("BOOK_VALUE_INVALID")
                if 0<price<1:
                    if size:self.depth[token][name][price]=size
                    else:self.depth[token][name].pop(price,None)
                self.tops[token]=tuple(self.number(row[key]) if row.get(key) is not None else None for key in ("best_bid","best_ask"))
                changed.add(token)
        for token in changed:
            if any(len(levels)>20000 for levels in self.depth[token].values()):raise ValueError("BOOK_TOO_LARGE")
            self.stamps[token]=(source_ms,received_ms)
        return changed

    def synchronized(self,token):
        # One exchange change can span multiple frames, each reporting the final
        # BBO/hash. Do not publish the intermediate depth or reconnect before
        # the following removals/updates have had a bounded chance to arrive.
        if token not in self.depth:return False
        bid=max(self.depth[token]["bids"],default=0);ask=min(self.depth[token]["asks"],default=1)
        return all(value is None or abs(value-actual)<=.000001 for value,actual in zip(self.tops.get(token,(None,None)),(bid,ask)))

    def quote(self,token):
        if not self.synchronized(token):raise ValueError("BOOK_DEPTH_MISMATCH")
        depth=self.depth[token];source,received=self.stamps[token]
        bids=[{"price":p,"size":depth["bids"][p]} for p in sorted(depth["bids"],reverse=True)[:50]]
        asks=[{"price":p,"size":depth["asks"][p]} for p in sorted(depth["asks"])[:50]]
        return {"token_id":token,"outcome":self.tokens[token],"bids":bids,"asks":asks,
                "best_bid":bids[0]["price"] if bids else None,"best_ask":asks[0]["price"] if asks else None,
                "bid_size":bids[0]["size"] if bids else None,"ask_size":asks[0]["size"] if asks else None,
                "source_ms":source,"received_ms":received,"updated_at_ms":source,"source":"clob-ws-live"}


class ReliableLiveBooks:
    def __init__(self,current,publish,quotes,rest_reader,connector=BufferedWebSocket.connect):
        self.current=current;self.publish=publish;self.quotes=quotes;self.rest_reader=rest_reader;self.connector=connector
        self.lock=threading.Lock();self.ws=None;self.resync=threading.Event();self.retry={};self.failures={}
        self.stats={"ws_state":"waiting","reconnections":0,"late_messages":0,"rest_requests":0,"rest_successes":0,
                    "last_ws_lag_ms":None,"last_reason":None,"last_rest_error":None,"last_rest_error_detail":None}

    def update(self,**values):
        with self.lock:self.stats.update(values)

    def increment(self,name):
        with self.lock:self.stats[name]+=1

    def request_resync(self,reason="运行恢复，重新同步行情"):
        self.update(last_reason=reason);self.resync.set()
        if self.ws:
            try:self.ws.close()
            except Exception:pass

    def repair_once(self,stop):
        market=self.current();now=time.time()
        if not market or market.ends_at<=now or not self.rest_reader:return
        observed=self.quotes()
        for side,token in (("Up",market.up_token),("Down",market.down_token)):
            if stop.is_set():return
            if fresh_book(observed.get(side,{}),token,now,source_age=2000,receive_age=2000):continue
            if now<self.retry.get(token,0):continue
            self.retry[token]=now+2;self.increment("rest_requests")
            try:
                book=self.rest_reader(token);finished=time.time()
                if stop.is_set():return
                if not fresh_book(book,token,finished):raise ValueError("REST_BOOK_NOT_FRESH")
                book={**book,"outcome":side,"source":"clob-rest-recovery","updated_at_ms":book["source_ms"],
                      "bid_size":book["bids"][0]["size"],"ask_size":book["asks"][0]["size"]}
                if self.publish(market,{side:book}):self.increment("rest_successes")
                self.failures[token]=0;self.update(last_rest_error=None,last_rest_error_detail=None)
            except Exception as exc:
                failures=self.failures.get(token,0)+1;self.failures[token]=failures
                self.retry[token]=time.time()+min(15,2**min(failures,4))
                # Only fixed messages produced by our read-only book validator
                # reach the UI; never expose arbitrary HTTP bodies or secrets.
                details={"官方盘口缺失或交叉":"官方盘口缺少买盘、卖盘或价格交叉，等待恢复",
                         "官方盘口过期，等待新盘口":"官方返回的盘口已过期，正在重新获取",
                         "官方盘口价格无效":"官方盘口价格无效，正在重新获取",
                         "官方盘口标识不匹配":"官方盘口与本期市场不匹配，正在重新获取",
                         "该市场使用新的交易协议，当前 SDK 暂不提交订单":"当前市场使用暂不支持的交易协议",
                         "REST_BOOK_NOT_FRESH":"官方盘口过期或不完整，正在重新获取"}
                detail=details.get(str(exc),"官方盘口查询暂时失败，正在自动重试")
                self.update(last_rest_error=type(exc).__name__,last_rest_error_detail=detail)
        active={market.up_token,market.down_token}
        self.retry={k:v for k,v in self.retry.items() if k in active};self.failures={k:v for k,v in self.failures.items() if k in active}

    def _repair_loop(self,parent_stop,local_stop):
        while not parent_stop.is_set() and not local_stop.is_set():
            try:self.repair_once(local_stop)
            except Exception as exc:self.update(last_rest_error=type(exc).__name__,last_rest_error_detail="盘口补齐暂时失败，正在自动重试")
            local_stop.wait(.25)

    def run(self,stop):
        local_stop=threading.Event();repair=threading.Thread(target=self._repair_loop,args=(stop,local_stop),name="live-book-repair",daemon=True)
        repair.start();backoff=.25
        try:
            while not stop.is_set():
                market=self.current()
                if market is None or market.ends_at<=time.time():stop.wait(.25);continue
                try:
                    self.resync.clear();self.update(ws_state="connecting")
                    self.ws=self.connector(CLOB_MARKET_WS_URL,8)
                    self.ws.send_text(json.dumps({"assets_ids":[market.up_token,market.down_token],"type":"market","custom_feature_enabled":True}))
                    book=LiveDepthBook(market);connected_at=last_ping=last_fresh=time.monotonic();last_publish=0.;pending=set();late_since=None;mismatches={}
                    while not stop.is_set():
                        current=self.current()
                        if current is None or current.round_id!=market.round_id or current.ends_at<=time.time():break
                        if self.resync.is_set():raise ValueError("BOOK_RESYNC_REQUESTED")
                        if time.monotonic()-last_fresh>6:raise ValueError("BOOK_STREAM_STALLED")
                        if time.monotonic()-last_ping>=10:self.ws.send_text("PING");last_ping=time.monotonic()
                        text=self.ws.read_text(.25)
                        if text and text not in ("PING","PONG"):
                            data=json.loads(text)
                            for message in data if isinstance(data,list) else [data]:
                                if not isinstance(message,dict) or message.get("event_type") not in ("book","price_change"):continue
                                source=float(message.get("timestamp",0));received=time.time()*1000
                                if not math.isfinite(source) or source<=0 or source>received+1000:raise ValueError("BOOK_TIMESTAMP_INVALID")
                                changed=book.apply(message,source,received)
                                if not changed:continue
                                for token in changed:
                                    if book.synchronized(token):mismatches.pop(token,None)
                                    else:mismatches.setdefault(token,time.monotonic())
                                lag=received-source;self.update(last_ws_lag_ms=round(lag,1))
                                if lag>3000:
                                    self.increment("late_messages")
                                    if late_since is None:late_since=time.monotonic()
                                    if time.monotonic()-late_since>=1:raise ValueError("BOOK_STREAM_BACKLOG")
                                else:
                                    late_since=None;last_fresh=time.monotonic();pending.update(changed)
                                    # A single fresh initial snapshot isn't a stable
                                    # connection. Keep backoff for repeatedly late streams.
                                    if time.monotonic()-connected_at>=10:backoff=.25
                        if mismatches and time.monotonic()-min(mismatches.values())>=1:raise ValueError("BOOK_DEPTH_MISMATCH")
                        if pending and time.monotonic()-last_publish>=.05:
                            ready={};deferred=set()
                            for token in pending:
                                if not book.synchronized(token):deferred.add(token);continue
                                quote=book.quote(token)
                                if fresh_book(quote,token,time.time()):ready[book.tokens[token]]=quote
                            if ready and self.publish(market,ready):self.update(ws_state="streaming")
                            pending=deferred;last_publish=time.monotonic()
                except Exception as exc:
                    reason=str(exc) if isinstance(exc,ValueError) and str(exc).startswith(("BOOK_","INITIAL_BOOK_")) else type(exc).__name__
                    self.update(ws_state="reconnecting",last_reason=reason);self.increment("reconnections")
                finally:
                    if self.ws:
                        try:self.ws.close()
                        except Exception:pass
                    self.ws=None
                if not stop.is_set():stop.wait(backoff);backoff=min(8,backoff*2)
        finally:
            local_stop.set();repair.join(timeout=6);self.update(ws_state="stopped")

    def health(self):
        market=self.current();observed=self.quotes();now=time.time()
        with self.lock:result=dict(self.stats)
        sides={}
        if market and market.ends_at>now:
            for side,token in (("Up",market.up_token),("Down",market.down_token)):
                quote=observed.get(side,{})
                source=quote.get("source_ms");sides[side]={"fresh":fresh_book(quote,token,now),"source":quote.get("source"),
                    "age_ms":round(now*1000-source,1) if isinstance(source,(int,float)) else None,
                    "receive_age_ms":round(now*1000-quote["received_ms"],1) if isinstance(quote.get("received_ms"),(int,float)) else None}
        result.update(sides=sides,fresh_sides=sum(s["fresh"] for s in sides.values()),checked_at=now)
        return result
