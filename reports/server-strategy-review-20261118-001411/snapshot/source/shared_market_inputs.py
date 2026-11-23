"""One verified market-input adapter for Paper and managed-live consumers.

No balances, orders, strategy thresholds or execution policy belong here.
"""
from collections import deque
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import statistics
import threading

from .optimized_reference import SPOT
from .strategy import input_from_snapshot, multi_source_price_context


def reference_error(market,reference,now):
    if market is None or market.target_price<=0:return "等待本期官方参考价"
    if not reference.get("ready") or not reference.get("protocol_verified"):
        return "等待同源行情和结算基准校验"
    spot=reference.get("latest",{}).get(SPOT,{})
    value=spot.get("price");stamp=spot.get("source_ms")
    if (not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0
            or not isinstance(stamp,(int,float)) or not 0<=now*1000-stamp<=4000):
        return "Chainlink 参考价未就绪"
    return None


def quote_error(market,quotes,side,now):
    if market is None:return "等待本期官方参考价"
    quote=quotes.get(side,{})
    token=market.up_token if side=="Up" else market.down_token
    source=quote.get("source_ms");received=quote.get("received_ms")
    if (str(quote.get("token_id"))!=token or not all(isinstance(v,(int,float)) for v in (source,received))
            or not 0<=now*1000-source<=3000 or not 0<=now*1000-received<=2500):
        return "本方向真实盘口过期或不匹配"
    bid=quote.get("best_bid");ask=quote.get("best_ask")
    if not isinstance(bid,(int,float)) or not isinstance(ask,(int,float)) or not 0<bid<=ask<1:
        return "本方向盘口缺失或交叉"
    return None


@dataclass(frozen=True)
class MarketInputs:
    market: object
    reference: dict
    price: dict
    quotes: dict
    observed_at: float
    snapshot_id: str
    error: str | None

    def strategy_input(self):
        return input_from_snapshot(self.market,{"price":self.price,"quotes":self.quotes},now_ts=self.observed_at)

    def quote_error(self,side,now=None):
        return quote_error(self.market,self.quotes,side,self.observed_at if now is None else now)

    def valid_quotes(self,now):
        return {side:quote for side,quote in self.quotes.items() if self.quote_error(side,now) is None}


class SharedMarketInputs:
    def __init__(self,feed,external_prices):
        self.feed=feed;self.external_prices=external_prices;self.lock=threading.RLock()
        self.basis={key:deque(maxlen=180) for key in ("okx","binance")};self.last_basis={}

    def capture(self,advance_basis=False):
        return self.build(*self.feed.observation(),advance_basis=advance_basis)

    def build(self,market,reference,quotes,now,*,advance_basis=True):
        reference=deepcopy(reference);quotes=deepcopy(quotes)
        error=reference_error(market,reference,now)
        # Do not turn receipt time into quote source time. The live collector is
        # authoritative, including for legacy Paper's updated_at_ms field.
        for quote in quotes.values():quote["updated_at_ms"]=quote.get("source_ms")
        spot=reference.get("latest",{}).get(SPOT,{})
        price={"chainlink":spot.get("price"),"chainlink_updated_ms":spot.get("source_ms"),"source":"PolyBolt:Chainlink"}
        value=spot.get("price")
        if not error and isinstance(value,(int,float)) and math.isfinite(value) and value>0:
            try:external=self.external_prices()
            except Exception:external={}
            with self.lock:
                # Same enrichment and history limits previously used by LiveSignals.
                for source,key in (("okx","okx"),("binance","binance_market")):
                    external_value=external.get(key);updated=external.get(key+"_updated_ms")
                    if (not isinstance(external_value,(int,float)) or not math.isfinite(external_value) or external_value<=0
                            or not isinstance(updated,(int,float)) or not 0<=now*1000-updated<=4500):continue
                    if advance_basis and self.last_basis.get(source)!=updated:
                        self.basis[source].append((updated,(external_value/value-1)*10000));self.last_basis[source]=updated
                    history=[(t,p) for t,p in self.basis[source] if t>=now*1000-90000]
                    if advance_basis:self.basis[source]=deque(history,maxlen=180)
                    price.update({key:external_value,key+"_updated_ms":updated,source+"_basis_samples":len(history),
                                  source+"_basis_median_bps":statistics.median(p for _,p in history) if history else None})
        price["multi_context"]=multi_source_price_context(price,int(now*1000))
        if market:
            price.update(target_price=market.target_price,target_price_source="shared:verified_opening",target_price_fallback=False)
        identity={"round":market.round_id if market else None,"target":market.target_price if market else None,
                  "price":price,"quotes":quotes,"reference":reference,"observed_at":now}
        snapshot_id=hashlib.sha256(json.dumps(identity,sort_keys=True,default=str).encode()).hexdigest()[:20]
        return MarketInputs(market,reference,price,quotes,now,snapshot_id,error)
