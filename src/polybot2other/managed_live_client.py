"""Official CLOB transport with durable-before-submit order identities."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import logging
import math
import os
import time

import httpx
from py_clob_client_v2.client import ClobClient as SDKClient
from py_clob_client_v2.exceptions import PolyApiException

from .live import PolymarketLiveClient
from .managed_live_recovery import (normalize_error, FINALIZE_AFTER_END_SECONDS,
                                    is_fak_no_match,response_acceptance_evidence,saved_fak_no_match)


class OrderConfirmationPending(RuntimeError):
    """An order read is incomplete; retain its identity and retry reads, never POST."""


class OrderSubmissionUncertain(RuntimeError):
    def __init__(self,details):
        super().__init__(details.get("error_message") or "提交结果待核对")
        self.details=details


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
        orders=[]
        for row in raw:
            if (not isinstance(row,dict) or not row.get("id") or str(row.get("side","")).upper() not in ("BUY","SELL")
                    or any(row.get(k) is None for k in ("original_size","size_matched","price"))):
                raise ValueError("官方挂单字段不完整")
            original=number(row["original_size"]);matched=number(row["size_matched"]);price=number(row["price"])
            if not 0<price<=1 or matched>original+.000001:raise ValueError("官方挂单金额或份额无效")
            orders.append({"id":str(row["id"]),"side":str(row["side"]).upper(),"remaining":max(0,original-matched),"price":price})
        return orders

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
        try:
            raw = client.post_order(prepared.signed,sdk["OrderType"].FAK)
        except PolyApiException as exc:
            body=getattr(exc,"error_msg",None);status=getattr(exc,"status_code",None)
            details={k:v for k,v in normalize_error(body).items() if k not in ("order_id","explicit_rejection")}
            details.update(status="UNKNOWN",http_status=status if isinstance(status,int) else None,error_type=type(exc).__name__)
            details.update(response_has_order_id=bool(normalize_error(body)["order_id"]),
                           response_acceptance_evidence=response_acceptance_evidence(body))
            rejected=self._definite_rejection(body,status)
            if rejected:return rejected
            raise OrderSubmissionUncertain(details) from None
        if not isinstance(raw,dict):raise OrderConfirmationPending("提交响应不完整，需核对订单结果")
        ident = raw.get("orderID") or raw.get("orderId") or raw.get("id")
        message=str(raw.get("errorMsg") or raw.get("error") or "").lower()
        rejected_reason=("订单金额或份额低于平台最低要求" if "minimum" in message else
                         "平台返回真实余额或授权不足" if "balance" in message or "allowance" in message else
                         "平台拒绝订单签名，请核对凭据与合约" if "signature" in message else
                         "订单价格或精度不符合平台要求" if "price" in message or "tick" in message else
                         "官方拒绝订单，未记入成交")
        diagnostic=normalize_error(raw) if message else {}
        return {"accepted":bool(raw.get("success") or ident),"external_id":ident,
                "identity_mismatch":bool(ident and str(ident).lower()!=prepared.external_id.lower()),
                "rejected":raw.get("success") is False and not ident,
                "rejected_reason":rejected_reason if raw.get("success") is False else None,
                "status":str(raw.get("status") or "PENDING"),
                "error_message":diagnostic.get("error_message"),"error_code":diagnostic.get("error_code"),
                "trade_ids":[str(t) for t in raw.get("tradeIDs",[]) if isinstance(t,str)] if isinstance(raw.get("tradeIDs"),list) else []}

    @staticmethod
    def _definite_rejection(raw,http_status):
        # Only documented client-side refusals are terminal. Timeouts, duplicates,
        # rate limits and unrecognized 5xx/bodies remain uncertain, never reposted.
        if http_status not in (400,401,403,500):return None
        parsed=normalize_error(raw)
        # An identifier in an HTTP error can identify the rejected attempt. The
        # documented FAK zero-match response is terminal unless the same body
        # also carries acceptance or fill evidence. Never confuse it with a timeout.
        if is_fak_no_match(parsed["error_message"],http_status) and not response_acceptance_evidence(raw):
            return {"accepted":False,"rejected":True,"external_id":None,"identity_mismatch":False,
                    "status":"REJECTED","http_status":400,"error_code":"FAK_NO_MATCH",
                    "error_message":parsed["error_message"],"rejected_reason":"FAK 无匹配成交，本笔未成交，预算已释放",
                    "trade_ids":[],"response_type":parsed["response_type"]}
        if parsed["order_id"]:return None
        if response_acceptance_evidence(raw):return None
        message=parsed["error_message"].lower().strip()
        if "duplicat" in message or "already" in message or "delayed" in message:return None
        if http_status==500:
            if message!="order timed out":return None
            # The documented exact response means rejection before reaching the book.
            return {"accepted":False,"rejected":True,"external_id":None,"identity_mismatch":False,"status":"REJECTED",
                    "http_status":500,"error_code":"TIMEOUT_NOT_ACCEPTED","error_message":parsed["error_message"],"rejected_reason":"平台确认处理超时，订单未进入订单簿","trade_ids":[]}
        rules=[(("no orders found to match with fak order",),"FAK_NO_MATCH","限价范围内没有可成交买卖盘，本笔未成交"),
               (("not enough balance / allowance","invalid_order_not_enough_balance"),"BALANCE_OR_ALLOWANCE","平台拒绝订单：余额或授权不足"),
               (("invalid signature","invalid_order_signature"),"INVALID_SIGNATURE","平台拒绝订单：签名无效"),
               (("invalid order payload",),"INVALID_PAYLOAD","平台拒绝订单：参数不符合要求"),
               (("invalid_order_min_size","invalid_order_min_tick_size"),"INVALID_SIZE_OR_TICK","平台拒绝订单：金额、份额或精度不符合要求"),
               (("unauthorized/invalid api key","invalid l1 request headers"),"AUTH_REJECTED","平台拒绝订单：身份认证失败"),
               (("the order owner has to be the owner of the api key","the order signer address has to be the address of the api key"),"IDENTITY_REJECTED","平台拒绝订单：签名人与 API 身份不匹配")]
        match=next(((code,reason) for prefixes,code,reason in rules if any(message.startswith(p) for p in prefixes)),None)
        if http_status==400 and message.startswith("order ") and " is invalid. " in message:
            if "breaks minimum tick size rule:" in message:match=("INVALID_TICK","平台拒绝订单：价格精度不符合市场要求")
            elif "lower than the minimum:" in message:match=("INVALID_SIZE","平台拒绝订单：份额低于市场最低要求")
        if not match and not parsed["explicit_rejection"]:return None
        code,reason=match or ("EXPLICIT_REJECTION","官方明确拒绝订单，未成交")
        return {"accepted":False,"rejected":True,"external_id":None,"identity_mismatch":False,
                "status":"REJECTED","http_status":http_status,"error_code":code,"error_message":parsed["error_message"],
                "rejected_reason":reason,"trade_ids":[],"response_type":parsed["response_type"]}

    def reconcile(self, order, market, info):
        sdk, client = self.client()
        remote_id=order.get("ack_id") or order["external_id"]
        evidence={"order":"unavailable","trades":"unavailable","open_orders":"not_needed","token_balance":None,"checks_started_at":time.time()}
        raw=None
        try:
            raw=client.get_order(remote_id)
            evidence["order"]="found" if isinstance(raw,dict) and raw.get("id") else "missing" if raw is None else "incomplete"
        except Exception as exc:evidence["order_error"]=type(exc).__name__
        if isinstance(raw,dict) and raw.get("id") and str(raw["id"]).lower()!=remote_id.lower():
            raise ValueError("官方订单编号与查询订单不一致")
        complete=isinstance(raw,dict) and bool(raw.get("id")) and bool(raw.get("status")) and raw.get("size_matched") is not None
        params = sdk["TradeParams"](market=market.condition_id,asset_id=order["token_id"],after=int(order["created_at"])-30)
        trades=[]
        try:
            found=client.get_trades(params,only_first_page=False)
            if isinstance(found,list):trades=found;evidence["trades"]="checked"
        except Exception as exc:evidence["trades_error"]=type(exc).__name__
        confirmed=[];seen=set();finished=set();pending=False;failed_shares=0.
        for trade in trades:
            if not isinstance(trade,dict):raise ValueError("官方成交记录格式无效")
            if str(trade.get("taker_order_id","")).lower()!=remote_id.lower():
                continue
            if str(trade.get("asset_id",order["token_id"]))!=order["token_id"]:
                raise ValueError("成交的资产与本地订单不一致")
            ident=str(trade.get("id") or "")
            if not ident:pending=True;continue
            if ident in seen:continue
            seen.add(ident)
            status=str(trade.get("status","")).upper()
            if status=="FAILED":
                failed_shares+=number(trade["size"])
                finished.add(ident)
                continue
            if status!="CONFIRMED":
                pending=True
                continue
            quantity=number(trade["size"]);price=number(trade["price"])
            finished.add(ident)
            fee=fee_amount(quantity,price,info["fee_rate"],info["fee_exponent"])
            confirmed.append({"trade_id":ident,"shares":quantity,"price":price,"fee":fee,
                              "fee_source":"official_market_fee_schedule"})
        status=str(raw.get("status","")).upper() if complete else "UNAVAILABLE"
        terminal=status in {"MATCHED","FILLED","CANCELED","CANCELLED","EXPIRED","UNMATCHED"}
        actual=sum(t["shares"] for t in confirmed)
        reconciled=bool(complete and terminal and evidence["trades"]=="checked" and not pending
                        and abs(actual+failed_shares-number(raw["size_matched"]))<.000001)
        evidence.update(matching_trades=len(seen),confirmed_shares=actual,pending_trades=pending)
        if confirmed:evidence["confirmed_fills"]=confirmed
        if not complete or order.get("state")=="QUARANTINED":
            evidence["open_orders"]="unavailable"
            try:
                opened=self.read_open_orders();evidence["open_orders"]="checked"
                evidence["matching_open_order"]=any(o["id"].lower()==remote_id.lower() for o in opened)
            except Exception as exc:evidence["open_orders_error"]=type(exc).__name__
            try:evidence["token_balance"]=self.token_balance(order["token_id"])["balance"]
            except Exception as exc:evidence["token_balance_error"]=type(exc).__name__
            # Missing lookup alone is never proof of a no-fill. A saved successful
            # FAK acknowledgement plus all its final receipts is independent proof.
            acknowledgement=json.loads(order.get("submission_json") or "{}")
            expected=set(acknowledgement.get("trade_ids") or [])
            if (not complete and acknowledgement.get("accepted") is True and acknowledgement.get("status","").upper()=="MATCHED"
                    and expected and expected==seen==finished and not pending and evidence["trades"]=="checked"
                    and evidence["open_orders"]=="checked" and evidence["matching_open_order"] is False):
                reconciled=True;status="CONFIRMED_RECEIPTS"
        if (order.get("state") in ("QUARANTINED","EXPIRED_UNFILLED") and not reconciled
                and not saved_fak_no_match(order)
                and time.time()-market.ends_at>=FINALIZE_AFTER_END_SECONDS and evidence["order"]=="missing"):
            try:evidence.update(self.wallet_activity_evidence(order,market))
            except Exception as exc:evidence.update(wallet_activity="unavailable",wallet_activity_error=type(exc).__name__)
        reason=("官方订单暂未同步，已交叉核对成交、挂单和持仓" if not complete else
                "官方成交查询暂不可用，继续核对" if evidence["trades"]!="checked" else "官方成交尚未全部确认")
        evidence["checks_completed_at"]=time.time()
        return {"fills":confirmed,"terminal":reconciled,"no_fill":reconciled and actual==0,"status":status,
                "pending_reason":None if reconciled else reason,"evidence":evidence}

    def wallet_activity_evidence(self,order,market):
        """Independent Data API history; require complete pagination and matching wallet/market."""
        from .managed_live_recovery import expected_redemption
        funder=os.environ.get("POLYBOT2OTHER_LIVE_FUNDER_ADDRESS","").strip().lower()
        if not funder:raise ValueError("缺少钱包身份")
        start=int(order["created_at"])-2;end=int(time.time());count=0;pages=0;offset=0;previous=None
        settlements=0;burned=0.;settlement_rows=[];seen_redemptions={};activity_types={}
        while offset<5000:
            response=self.http.get(self.settings.data_api_url.rstrip('/')+'/activity',params={
                "user":funder,"market":market.condition_id,"start":start,"end":end,"limit":100,"offset":offset,
                "sortBy":"TIMESTAMP","sortDirection":"DESC"})
            response.raise_for_status();rows=response.json();pages+=1
            if not isinstance(rows,list):raise ValueError("钱包活动响应不完整")
            fingerprint=json.dumps(rows,sort_keys=True)
            if rows and fingerprint==previous:raise ValueError("钱包活动分页未前进")
            previous=fingerprint
            for row in rows:
                if (not isinstance(row,dict) or str(row.get("proxyWallet","")).lower()!=funder
                        or str(row.get("conditionId","")).lower()!=market.condition_id.lower()
                        or not isinstance(row.get("timestamp"),(int,float)) or isinstance(row.get("timestamp"),bool)
                        or not math.isfinite(row["timestamp"]) or not isinstance(row.get("type"),str) or not row["type"]
                        or (row.get("asset") is not None and not isinstance(row["asset"],str))):
                    raise ValueError("钱包活动身份或市场字段不匹配")
                if row["timestamp"]>=start and (not row.get("asset") or str(row["asset"])==order["token_id"]):
                    ident=(row.get("transactionHash"),row.get("asset"))
                    settlement={k:row.get(k) for k in ("type","timestamp","asset","size","usdcSize","transactionHash")}
                    if row["type"]=="REDEEM" and ident in seen_redemptions:
                        if settlement!=seen_redemptions[ident]:raise ValueError("同一赎回记录的份额不一致")
                        continue
                    if expected_redemption(order,market,row,burned):
                        seen_redemptions[ident]=settlement;settlements+=1;burned+=row["size"]
                        settlement_rows.append(settlement)
                    else:
                        count+=1;activity_types[row["type"]]=activity_types.get(row["type"],0)+1
            if not rows or count:
                return {"wallet_activity":"checked","wallet_activity_count":count,"wallet_activity_pages":pages,
                        "wallet_activity_since":start,"wallet_activity_until":end,"wallet_activity_checked_at":time.time(),
                        "wallet_activity_complete":not rows,"wallet_activity_types":activity_types,
                        "wallet_settlement_count":settlements,"wallet_settlement_shares":round(burned,6),
                        "wallet_settlements":settlement_rows}
            offset+=len(rows)
        raise ValueError("钱包活动分页尚未完成")

    def token_balance(self, token_id):
        sdk,client=self.client()
        params=self.legacy._balance_allowance_params(sdk,sdk["AssetType"].CONDITIONAL,token_id=token_id)
        raw=client.get_balance_allowance(params)
        allowance=max([fixed_collateral(x) for x in (raw.get("allowances") or {}).values()]+[fixed_collateral(raw.get("allowance",0))])
        return {"balance":fixed_collateral(raw["balance"]),"allowance":allowance}
