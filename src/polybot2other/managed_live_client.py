"""Official CLOB transport with durable-before-submit order identities."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import logging
import math
import os
import time

import httpx
from py_clob_client_v2.client import ClobClient as SDKClient
from py_clob_client_v2.exceptions import PolyApiException

from .live import PolymarketLiveClient


class OrderConfirmationPending(RuntimeError):
    """An order read is incomplete; retain its identity and retry reads, never POST."""


def number(value):
    result = float(Decimal(str(value)))
    if not math.isfinite(result) or result < 0:
        raise ValueError("官方金额字段无效")
    return result


def fixed_collateral(value):
    # Balance/allowance endpoints use six-decimal integer collateral units.
    return float(Decimal(str(value)) / Decimal(1_000_000))


def fee_amount(shares, price, rate, exponent):
    return round(shares * rate * (price * (1-price)) ** exponent, 6)


def maximum_fee_ratio(rate, exponent, tick, ceiling):
    if rate==0:return 0.0
    if exponent<1:raise ValueError("暂不支持该市场的手续费曲线")
    if exponent==1:return rate  # Also covers a better fill below the cached tick.
    candidates = [tick, ceiling]
    if exponent > 1:
        critical = (exponent-1)/(2*exponent-1)
        if tick <= critical <= ceiling:
            candidates.append(critical)
    return max(rate * (p*(1-p))**exponent/p for p in candidates)


@dataclass
class PreparedOrder:
    external_id: str
    signed: object
    action: str
    token_id: str


class ManagedClobClient(SDKClient):
    """Keep official signing/authentication, use a bounded per-feature HTTP transport."""
    def __init__(self, transport, **kwargs):
        super().__init__(**kwargs)
        self._transport=transport

    def _request(self, method, endpoint, headers=None, data=None, params=None):
        headers={"Accept":"application/json","Content-Type":"application/json",**(headers or {})}
        for attempt in range(2 if method=="GET" else 1):
            try:
                kwargs={"method":method,"url":endpoint,"headers":headers,"params":params}
                if isinstance(data,str):kwargs["content"]=data.encode("utf-8")
                elif data is not None:kwargs["json"]=data
                response=self._transport.request(**kwargs)
                if response.status_code!=200:raise PolyApiException(response)
                return response.json()
            except httpx.RequestError:
                if method!="GET" or attempt:raise
                time.sleep(.15)

    def _get(self,endpoint,headers=None,params=None):
        return self._request("GET",endpoint,headers=headers,params=params)

    def _post(self,endpoint,headers=None,data=None,params=None):
        return self._request("POST",endpoint,headers=headers,data=data,params=params)

    def _delete(self,endpoint,headers=None,data=None,params=None):
        return self._request("DELETE",endpoint,headers=headers,data=data,params=params)


class ManagedExchange:
    """No orders are signed or posted by wallet/status reads."""
    def __init__(self, settings):
        self.settings = settings
        self.legacy = PolymarketLiveClient(settings)
        self.http = httpx.Client(timeout=httpx.Timeout(5, connect=3), follow_redirects=False, http2=False,
                                 limits=httpx.Limits(max_connections=20,max_keepalive_connections=10,keepalive_expiry=60))
        self._market_info = {}
        # SDK exceptions can include raw response bodies; expose only our sanitized errors.
        logging.getLogger("py_clob_client_v2.http_helpers.helpers").setLevel(logging.CRITICAL)

    @property
    def identity(self):
        funder = os.environ.get("POLYBOT2OTHER_LIVE_FUNDER_ADDRESS", "").strip().lower()
        if not funder:
            raise ValueError("钱包凭据尚未配置")
        return hashlib.sha256(f"{self.settings.live_trading_chain_id}:{funder}".encode()).hexdigest()

    def client(self):
        if self.legacy._credential_mode()!="env_api_creds":
            raise ValueError("请配置完整的现有 CLOB API 凭据")
        sdk = dict(self.legacy._sdk())
        sdk["ClobClient"] = lambda **kwargs: ManagedClobClient(self.http,**kwargs)
        return sdk, self.legacy._authenticated_client(sdk)

    def read_wallet(self):
        started = time.time()
        result = {"balance": None, "allowance": None, "portfolio_value": None,
                  "total_equity": None, "checked_at": None, "read_started_at": started,
                  "ready": False, "error": None}
        try:
            sdk, client = self.client()
            params = self.legacy._balance_allowance_params(sdk, sdk["AssetType"].COLLATERAL)
            raw = client.get_balance_allowance(params)
            if not isinstance(raw, dict) or "balance" not in raw:
                raise ValueError("余额响应格式无效")
            balance = fixed_collateral(raw["balance"])
            allowances = raw.get("allowances") or {}
            allowance = max([fixed_collateral(v) for v in allowances.values()] + [fixed_collateral(raw.get("allowance", 0))])
            if not math.isfinite(balance) or balance < 0 or not math.isfinite(allowance):
                raise ValueError("钱包金额无效")
            funder = os.environ["POLYBOT2OTHER_LIVE_FUNDER_ADDRESS"].strip()
            result.update(balance=balance, allowance=allowance, checked_at=time.time(), ready=True,
                          address_masked=funder[:6]+"…"+funder[-4:], currency="pUSD")
            try:
                value = self.portfolio_value(funder)
                result.update(portfolio_value=value, total_equity=round(balance+value,6))
            except Exception:
                result["valuation_error"] = "钱包余额已读取，持仓总估值暂不可用"
        except Exception as exc:
            result["error"] = f"真实余额暂不可用（{type(exc).__name__}），等待重试"
        return result

    def portfolio_value(self, funder):
        # Both versions are official read-only endpoints. Never treat an error as 0.
        for path in ("/v2/value", "/value"):
            try:
                response=self.http.get(self.settings.data_api_url.rstrip("/")+path,params={"user":funder})
                response.raise_for_status();payload=response.json()
                rows=[payload.get("data")] if isinstance(payload,dict) else payload
                for row in rows or []:
                    if isinstance(row,dict) and str(row.get("proxy_wallet",row.get("user",""))).lower()==funder.lower():
                        return number(row["value"])
            except Exception:
                continue
        raise ValueError("真实持仓估值暂不可用")

    def read_geo(self):
        result = {"checked_at": time.time(), "ready": False, "blocked": None}
        try:
            response = self.http.get("https://polymarket.com/api/geoblock")
            response.raise_for_status()
            data = response.json()
            if not isinstance(data.get("blocked"), bool):
                raise ValueError("地区状态响应不完整")
            result.update(ready=True, blocked=data["blocked"])
            if data["blocked"]:
                result["error"] = "Polymarket 当前地区限制禁止下单"
        except Exception as exc:
            result["error"] = f"暂无法确认交易地区状态（{type(exc).__name__}）"
        return result

    def read_open_orders(self):
        _, client = self.client()
        raw = client.get_open_orders()
        if not isinstance(raw, list):
            raise ValueError("官方挂单响应无效")
        return [{"id": str(r.get("id", "")), "side": str(r.get("side", "")),
                 "remaining": max(0, number(r.get("original_size",0))-number(r.get("size_matched",0))),
                 "price": number(r.get("price",0))} for r in raw]

    def market_info(self, market, force=False):
        key = market.round_id
        if not force and key in self._market_info:
            return self._market_info[key]
        _, client = self.client()
        raw = client.get_clob_market_info(market.condition_id)
        tokens = {str(t["t"]) for t in raw.get("t", []) if isinstance(t,dict) and t.get("t")}
        if not {market.up_token, market.down_token} <= tokens:
            raise ValueError("官方市场标识不匹配")
        fee = raw.get("fd")
        if not isinstance(fee,dict) or "r" not in fee or "e" not in fee:
            raise ValueError("缺少官方费率参数")
        info = {"fee_rate": number(fee["r"]), "fee_exponent": number(fee["e"]),
                "tick_size": str(raw["mts"]), "neg_risk": bool(raw.get("nr", False))}
        if not 0 <= info["fee_rate"] <= 1 or not 0 <= info["fee_exponent"] <= 4:
            raise ValueError("不支持的官方费率参数")
        self._market_info[key] = info
        return info

    def rest_book(self, token_id):
        return self._read_book(token_id,display_only=False)

    def valuation_book(self, token_id):
        """Read-only display refresh. This result must never be used to place orders."""
        return self._read_book(token_id,display_only=True)

    def _read_book(self, token_id, *, display_only):
        response = self.http.get(self.settings.clob_url.rstrip("/")+"/book", params={"token_id": token_id})
        response.raise_for_status()
        raw = response.json()
        if raw.get("version") not in (None,"","ctf","v1"):
            raise ValueError("该市场使用新的交易协议，当前 SDK 暂不提交订单")
        if str(raw.get("asset_id")) != str(token_id):
            raise ValueError("官方盘口标识不匹配")
        bids = sorted([{"price":number(x["price"]),"size":number(x["size"])} for x in raw.get("bids",[]) if number(x["size"])>0],key=lambda x:-x["price"])
        asks = sorted([{"price":number(x["price"]),"size":number(x["size"])} for x in raw.get("asks",[]) if number(x["size"])>0],key=lambda x:x["price"])
        stamp = number(raw.get("timestamp",0))
        now = time.time()
        if not 0 <= now*1000-stamp <= (30000 if display_only else 3000):
            raise ValueError("官方盘口过期，等待新盘口")
        if any(not 0<level["price"]<1 for level in bids+asks):
            raise ValueError("官方盘口价格无效")
        if not bids or (not asks and not display_only) or (asks and bids[0]["price"] >= asks[0]["price"]):
            raise ValueError("官方盘口缺失或交叉")
        return {"token_id":str(token_id),"bids":bids,"asks":asks,"best_bid":bids[0]["price"],
                "best_ask":asks[0]["price"] if asks else None,"source_ms":stamp,"received_ms":now*1000,
                "updated_at_ms":stamp,"min_order_size":number(raw.get("min_order_size",0)),
                "tick_size":str(raw.get("tick_size","0.01")),"neg_risk":bool(raw.get("neg_risk",False))}

    def prepare(self, market, token_id, action, amount, limit, info):
        if not self.settings.live_trading_runtime_enabled:
            raise ValueError("进程级实盘功能未启用")
        if self.legacy.readiness_errors():
            raise ValueError("实盘凭据或 SDK 尚未就绪")
        sdk, client = self.client()
        from py_clob_client_v2.order_builder.builder import get_contract_config, ExchangeOrderBuilderV1, ExchangeOrderBuilderV2
        if action == "BUY":
            ratio = maximum_fee_ratio(info["fee_rate"],info["fee_exponent"],float(info["tick_size"]),limit)
            notional = math.floor(amount/(1+ratio)*100)/100
            if notional <= 0:
                raise ValueError("含费用预算小于交易所金额精度")
        else:
            notional = math.floor(amount*1_000_000)/1_000_000
        arguments={"token_id":str(token_id),"amount":notional,"side":sdk["Side"].BUY if action=="BUY" else sdk["Side"].SELL,
                   "price":limit,"order_type":sdk["OrderType"].FAK}
        if action=="BUY":arguments["user_usdc_balance"]=amount
        args=sdk["MarketOrderArgs"](**arguments)
        options=sdk["PartialCreateOrderOptions"](tick_size=info["tick_size"],neg_risk=info["neg_risk"])
        signed = client.create_market_order(args,options)
        if int(signed.makerAmount)<=0 or int(signed.takerAmount)<=0:
            raise ValueError("订单金额或份额小于交易所签名精度")
        contracts = get_contract_config(client.signer.get_chain_id())
        is_v2 = hasattr(signed,"timestamp")
        if not is_v2:
            raise ValueError("当前自动实盘仅支持已核对的 V2 成交与费用口径")
        contract = (contracts.neg_risk_exchange_v2 if info["neg_risk"] else contracts.exchange_v2) if is_v2 else (contracts.neg_risk_exchange if info["neg_risk"] else contracts.exchange)
        builder = (ExchangeOrderBuilderV2 if is_v2 else ExchangeOrderBuilderV1)(contract,client.signer.get_chain_id(),client.signer)
        ident = str(builder.build_order_hash(builder.build_order_typed_data(signed)))
        if not ident.startswith("0x") or len(ident)!=66:
            raise ValueError("无法在提交前确定官方订单哈希")
        return PreparedOrder(ident,signed,action,str(token_id))

    def submit(self, prepared):
        # Called once, only after PREPARED/SUBMITTING is durable. Never blind-retry POST.
        if not self.settings.live_trading_runtime_enabled:
            raise ValueError("进程级实盘功能未启用")
        sdk, client = self.client()
        raw = client.post_order(prepared.signed,sdk["OrderType"].FAK)
        ident = raw.get("orderID") or raw.get("orderId") or raw.get("id")
        message=str(raw.get("errorMsg") or raw.get("error") or "").lower()
        rejected_reason=("订单金额或份额低于平台最低要求" if "minimum" in message else
                         "平台返回真实余额或授权不足" if "balance" in message or "allowance" in message else
                         "平台拒绝订单签名，请核对凭据与合约" if "signature" in message else
                         "订单价格或精度不符合平台要求" if "price" in message or "tick" in message else
                         "官方拒绝订单，未记入成交")
        return {"accepted":bool(raw.get("success") or ident),"external_id":ident,
                "identity_mismatch":bool(ident and str(ident).lower()!=prepared.external_id.lower()),
                "rejected":raw.get("success") is False and not ident,
                "rejected_reason":rejected_reason,
                "status":str(raw.get("status") or "PENDING")}

    def reconcile(self, order, market, info):
        sdk, client = self.client()
        remote_id=order.get("ack_id") or order["external_id"]
        raw = client.get_order(remote_id)
        if not isinstance(raw,dict) or not raw.get("id"):
            raise OrderConfirmationPending("官方订单暂未同步，等待确认")
        if str(raw["id"]).lower()!=remote_id.lower():
            raise ValueError("官方订单编号与查询订单不一致")
        if not raw.get("status") or raw.get("size_matched") is None:
            raise OrderConfirmationPending("官方订单状态暂不完整，等待确认")
        params = sdk["TradeParams"](market=market.condition_id,asset_id=order["token_id"],after=int(order["created_at"])-30)
        trades = client.get_trades(params,only_first_page=False)
        if not isinstance(trades,list):
            raise OrderConfirmationPending("官方成交列表暂不可确认")
        confirmed=[];seen=set();pending=False;failed_shares=0.
        for trade in trades:
            if str(trade.get("taker_order_id","")).lower()!=remote_id.lower():
                continue
            if str(trade.get("asset_id",order["token_id"]))!=order["token_id"]:
                raise ValueError("成交的资产与本地订单不一致")
            ident=str(trade.get("id") or "")
            if not ident or ident in seen:continue
            seen.add(ident)
            status=str(trade.get("status","")).upper()
            if status=="FAILED":
                failed_shares+=number(trade["size"])
                continue
            if status!="CONFIRMED":
                pending=True
                continue
            quantity=number(trade["size"]);price=number(trade["price"])
            fee=fee_amount(quantity,price,info["fee_rate"],info["fee_exponent"])
            confirmed.append({"trade_id":ident,"shares":quantity,"price":price,"fee":fee,
                              "fee_source":"official_market_fee_schedule"})
        matched=number(raw.get("size_matched",0))
        status=str(raw.get("status","")).upper()
        terminal=status in {"MATCHED","FILLED","CANCELED","CANCELLED","EXPIRED","UNMATCHED"}
        actual=sum(t["shares"] for t in confirmed)
        reconciled = terminal and not pending and abs(actual+failed_shares-matched)<.000001
        return {"fills":confirmed,"terminal":reconciled,"no_fill":reconciled and actual==0,"status":status}

    def token_balance(self, token_id):
        sdk,client=self.client()
        params=self.legacy._balance_allowance_params(sdk,sdk["AssetType"].CONDITIONAL,token_id=token_id)
        raw=client.get_balance_allowance(params)
        allowance=max([fixed_collateral(x) for x in (raw.get("allowances") or {}).values()]+[fixed_collateral(raw.get("allowance",0))])
        return {"balance":fixed_collateral(raw["balance"]),"allowance":allowance}
