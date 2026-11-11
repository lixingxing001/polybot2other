"""Read-only comparison of the running feed and the new book receiver. Never starts a bot."""
from dataclasses import replace
from pathlib import Path
import json
import threading
import time
import urllib.request

from polybot2other.config import load_settings
from polybot2other.models import MarketRound
from polybot2other.managed_live_book import ReliableLiveBooks,fresh_book
from polybot2other.managed_live_client import ManagedExchange

ROOT=Path(__file__).resolve().parents[2]
lock=threading.RLock();state={"market":None,"quotes":{}};stop=threading.Event()
settings=replace(load_settings(),live_trading_runtime_enabled=False)
exchange=ManagedExchange(settings);original_request=exchange.http.request
def read_only(method,*args,**kwargs):
    if method.upper()!="GET":raise AssertionError("Shadow feed forbids financial writes")
    return original_request(method,*args,**kwargs)
exchange.http.request=read_only
def current():
    with lock:return state["market"]
def quotes():
    with lock:return dict(state["quotes"])
def publish(market,updates):
    with lock:
        if stop.is_set() or not state["market"] or state["market"].round_id!=market.round_id:return False
        changed=False
        for side,q in updates.items():
            if state["quotes"].get(side,{}).get("source_ms",0)>q["source_ms"]:continue
            state["quotes"][side]=q;changed=True
        return changed
stream=ReliableLiveBooks(current,publish,quotes,exchange.rest_book)
worker=threading.Thread(target=stream.run,args=(stop,),daemon=True);worker.start()
samples=[];started=time.time();forced=False
try:
    while time.time()-started<90:
        live=json.load(urllib.request.urlopen('http://127.0.0.1:8791/api/status',timeout=10))["runtime"]["managed_live"]
        raw=live["market_data"]["market"]
        if raw:
            market=MarketRound(**raw)
            with lock:
                if state["market"] is None or state["market"].round_id!=market.round_id:state["quotes"]={}
                state["market"]=market
            now=time.time();old=live["market_data"].get("quotes",{});new=quotes()
            def old_fresh(q):
                return (isinstance(q.get("source_ms"),(int,float)) and isinstance(q.get("received_ms"),(int,float))
                        and 0<=now*1000-q["source_ms"]<=3000 and 0<=now*1000-q["received_ms"]<=2500
                        and isinstance(q.get("best_bid"),(int,float)) and isinstance(q.get("best_ask"),(int,float))
                        and 0<q["best_bid"]<=q["best_ask"]<1)
            row={"at":now,"left":market.ends_at-now,"old_fresh":sum(old_fresh(q) for q in old.values()),
                 "new_fresh":sum(fresh_book(new.get(side,{}),token,now) for side,token in [("Up",market.up_token),("Down",market.down_token)]),
                 "new_sources":{s:q["source"] for s,q in new.items()},"health":stream.health()}
            samples.append(row)
            if len(samples)%10==0:print(json.dumps({k:row[k] for k in ["at","old_fresh","new_fresh","new_sources"]}),flush=True)
        if not forced and time.time()-started>35:
            stream.request_resync("隔离验证：模拟一次推送重连");forced=True
        time.sleep(1)
finally:
    stop.set();stream.request_resync("隔离验证结束");worker.join(timeout=8);exchange.http.close()
valid=[r for r in samples if settings.min_time_left_seconds<=r["left"]<=settings.max_time_left_seconds and r["at"]-started>3]
result={"read_only":True,"simulated_disconnect_only_in_shadow":forced,"samples":samples,"eligible_samples":len(valid),
        "old_both_fresh_pct":round(100*sum(r["old_fresh"]==2 for r in valid)/len(valid),2) if valid else None,
        "new_both_fresh_pct":round(100*sum(r["new_fresh"]==2 for r in valid)/len(valid),2) if valid else None}
(ROOT/'reports/live-feed-reliability-20261102/shadow-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
print('RESULT',json.dumps({k:v for k,v in result.items() if k!="samples"}),flush=True)
