"""Evidence requirements for closing missing, expired orders; no trading side effects."""
import json
import math
import os
import re

FINALIZE_AFTER_END_SECONDS=600
FINALIZE_MIN_CHECKS=3
FINALIZE_MIN_SPAN_SECONDS=30
FINAL_AUDIT_INTERVAL_SECONDS=300
LATE_AUDIT_FAULT="到期核对后出现新的订单或成交证据，已恢复预留，需要复核"
FAK_NO_MATCH_SHORT="no orders found to match with fak order"
FAK_NO_MATCH_FULL=FAK_NO_MATCH_SHORT+". fak orders are partially filled or killed if no match is found"


def is_fak_no_match(message,http_status):
    """Only the documented HTTP 400 zero-match reply, not arbitrary error text."""
    return http_status==400 and " ".join(str(message or "").lower().split()).rstrip(".") in (FAK_NO_MATCH_SHORT,FAK_NO_MATCH_FULL)


def response_acceptance_evidence(raw,depth=0):
    """Conflicting acceptance/fill evidence takes precedence over rejection text."""
    if depth>5:return True
    if isinstance(raw,str):
        try:decoded=json.loads(raw)
        except (ValueError,TypeError):return False
        return response_acceptance_evidence(decoded,depth+1) if isinstance(decoded,(dict,list)) else False
    if isinstance(raw,list):return any(response_acceptance_evidence(v,depth+1) for v in raw)
    if not isinstance(raw,dict):return False
    for key,value in raw.items():
        name=re.sub(r"[^a-z0-9]","",str(key).lower())
        if name in ("success","ok","accepted") and value is True:return True
        if name in ("status","state") and str(value).lower() in ("accepted","matched","delayed","live","pending","filled","partial","partially_filled"):return True
        if name in ("tradeids","transactionshashes","transactionhashes") and value:return True
        if name in ("sizematched","filledsize","filledshares","makingamount","takingamount"):
            try:
                amount=float(value)
                if not math.isfinite(amount) or amount>0:return True
            except (TypeError,ValueError):
                if value not in (None,""):return True
        if isinstance(value,(dict,list)) and response_acceptance_evidence(value,depth+1):return True
        if name in ("error","errormsg","detail","errors","response") and isinstance(value,str) and response_acceptance_evidence(value,depth+1):return True
    return False


def saved_fak_no_match(order):
    try:receipt=json.loads(order.get("submission_json") or "{}")
    except (ValueError,TypeError):return False
    return (isinstance(receipt,dict) and is_fak_no_match(receipt.get("error_message"),receipt.get("http_status"))
            and not response_acceptance_evidence(receipt)
            and not receipt.get("response_acceptance_evidence"))


def finalization_evidence(order):
    try:proof=json.loads(order.get("finalization_json") or "{}")
    except (TypeError,ValueError):return {}
    if (not isinstance(proof,dict) or proof.get("policy")!="resolved-market-no-activity-v1"
            or proof.get("consecutive_checks",0)<FINALIZE_MIN_CHECKS
            or not order.get("finalized_at") or proof.get("last_check_at")!=order["finalized_at"]):return {}
    evidence=proof.get("evidence",{})
    return evidence if isinstance(evidence,dict) else {}


def expected_redemption(order,market,row,already_burned=0):
    """A later burn of SELL holdings proved intact at finalization is not a fill.

    BUY audits and pre-finalization redemptions remain conflicting evidence.
    An explicit asset, valid payout and bounded total shares are required.
    """
    proof=finalization_evidence(order)
    if (order.get("action")!="SELL" or not order.get("position_id") or order.get("ack_id")
            or any(order.get(k,0) for k in ("confirmed_shares","confirmed_notional","confirmed_fee"))
            or proof.get("market_resolved") is not True or proof.get("owned_position_open") is not True
            or proof.get("resolution_outcome") not in ("Up","Down")
            or row.get("type")!="REDEEM" or row.get("asset")!=order.get("token_id")
            or not isinstance(row.get("transactionHash"),str) or not row["transactionHash"]):return False
    values=[row.get("size"),row.get("usdcSize"),row.get("timestamp"),proof.get("token_balance"),proof.get("owned_token_shares")]
    if not all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in values):return False
    size,cash,stamp,balance,owned=values
    if not (size>0 and owned>0 and balance+.000001>=owned and already_burned+size<=balance+.000001
            and stamp>max(order["finalized_at"],market.ends_at)):return False
    payout=size if order["outcome"]==proof["resolution_outcome"] else 0
    return abs(cash-payout)<=.000001 and (not row.get("outcome") or row["outcome"]==order["outcome"])


def direct_order_evidence(evidence):
    return (evidence.get("order")=="found" or evidence.get("matching_trades",0)>0
            or evidence.get("matching_open_order") is True or bool(evidence.get("confirmed_fills"))
            or evidence.get("confirmed_shares",0)>0)


def fresh_absent_order(evidence,now):
    start=evidence.get("checks_started_at");end=evidence.get("checks_completed_at")
    return (all(isinstance(v,(int,float)) and math.isfinite(v) for v in (start,end))
            and start<=end<=now and now-start<=30 and now-end<=15
            and evidence.get("order")=="missing" and evidence.get("trades")=="checked"
            and evidence.get("matching_trades")==0 and evidence.get("pending_trades") is False
            and evidence.get("open_orders")=="checked" and evidence.get("matching_open_order") is False
            and not direct_order_evidence(evidence))


def settled_activity_only(order,evidence,now):
    proof=finalization_evidence(order)
    balance=evidence.get("token_balance");burned=evidence.get("wallet_settlement_shares")
    original=proof.get("token_balance")
    return (order.get("action")=="SELL" and fresh_absent_order(evidence,now)
            and evidence.get("wallet_activity")=="checked" and evidence.get("wallet_activity_complete") is True
            and evidence.get("wallet_activity_count")==0 and evidence.get("wallet_settlement_count",0)>0
            and all(isinstance(v,(int,float)) and math.isfinite(v) and v>=0 for v in (balance,burned,original))
            and abs(balance+burned-original)<=.000001)


def wallet_only_audit_fault(order):
    try:previous=json.loads(order.get("verification_json") or "{}")
    except (TypeError,ValueError):return False
    return (bool(finalization_evidence(order)) and not order.get("ack_id")
            and not any(order.get(k,0) for k in ("confirmed_shares","confirmed_notional","confirmed_fee"))
            and previous.get("wallet_activity_count",0)>0 and not direct_order_evidence(previous))


def safe_error_text(value):
    text=str(value or "")
    for key,secret in os.environ.items():
        if len(secret)>=8 and any(tag in key.upper() for tag in ("PRIVATE_KEY","API_KEY","API_SECRET","PASSPHRASE")):
            text=re.sub(re.escape(secret),"[REDACTED]",text,flags=re.IGNORECASE)
    text=re.sub(r"(?i)([\"']?(?:authorization|api[_ -]?key|api[_ -]?secret|private[_ -]?key|passphrase|signature)[\"']?\s*[=:]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",r"\1[REDACTED]",text)
    text=re.sub(r"0x[0-9a-fA-F]{128,}","[REDACTED_SIGNATURE]",text)
    text=re.sub(r"<[^>]*>"," ",text)
    return " ".join(text.split())[:1200]


def _safe_structure(value,depth=0):
    if depth>4:return "[TRUNCATED]"
    if isinstance(value,dict):
        result={}
        for key,item in list(value.items())[:20]:
            label=re.sub(r"[^a-z0-9]","",str(key).lower())
            sensitive=any(s in label for s in ("privatekey","apikey","apisecret","passphrase","signature","authorization"))
            result[str(key)]=("[REDACTED]" if sensitive or label in ("headers","request","body","payload","order","signedorder")
                              else _safe_structure(item,depth+1))
        return result
    if isinstance(value,list):return [_safe_structure(v,depth+1) for v in value[:20]]
    return safe_error_text(value) if isinstance(value,str) else value if value is None or isinstance(value,(int,float,bool)) else type(value).__name__


def normalize_error(raw):
    """Accept legacy text/JSON/nested errors without persisting response/request objects."""
    payload=raw;code=None;order_id=None;explicit=False
    for _ in range(5):
        if isinstance(payload,str):
            try:
                decoded=json.loads(payload)
                if isinstance(decoded,(dict,list,str)) and decoded!=payload:payload=decoded;continue
            except (ValueError,TypeError):pass
            break
        if isinstance(payload,list) and len(payload)==1:payload=payload[0];continue
        if not isinstance(payload,dict):payload=json.dumps(_safe_structure(payload),ensure_ascii=False);break
        code=payload.get("code") or payload.get("errorCode") or code
        order_id=payload.get("orderID") or payload.get("orderId") or payload.get("id") or order_id
        explicit=explicit or payload.get("success") is False or payload.get("ok") is False
        nested=next((payload[k] for k in ("error","errorMsg","message","detail","msg","reason","description","errors") if payload.get(k)),None)
        if nested is None:payload=json.dumps(_safe_structure(payload),ensure_ascii=False);break
        payload=nested
    message=safe_error_text(payload if isinstance(payload,(str,int,float)) else "平台返回未识别的错误结构")
    return {"error_message":message,"error_code":safe_error_text(code)[:100] if isinstance(code,(str,int)) else None,
            "order_id":order_id,"explicit_rejection":explicit,"response_type":type(raw).__name__}


def no_activity_candidate(order,market,evidence,now):
    """All successful independent reads are required. A missing/error read is insufficient."""
    if order["state"]!="QUARANTINED" or order.get("ack_id"):return False
    if any(order.get(k,0) for k in ("confirmed_shares","confirmed_notional","confirmed_fee")):return False
    if now-market.ends_at<FINALIZE_AFTER_END_SECONDS:return False
    start=evidence.get("checks_started_at");end=evidence.get("checks_completed_at")
    if not all(isinstance(t,(int,float)) and math.isfinite(t) for t in (start,end)):return False
    if not start<=end<=now or now-start>30 or now-end>15:return False
    if not (evidence.get("wallet_verified") is True and evidence.get("market_resolved") is True
            and evidence.get("resolution_outcome") in ("Up","Down")
            and evidence.get("order")=="missing" and evidence.get("trades")=="checked"
            and evidence.get("matching_trades")==0 and evidence.get("pending_trades") is False
            and evidence.get("open_orders")=="checked" and evidence.get("matching_open_order") is False
            and evidence.get("wallet_activity")=="checked" and evidence.get("wallet_activity_count")==0
            and evidence.get("wallet_activity_complete") is True):return False
    balance=evidence.get("token_balance")
    if not isinstance(balance,(int,float)) or not math.isfinite(balance) or balance<0:return False
    if order["action"]=="BUY":return order.get("position_id") is None and balance<=.000001
    expected=evidence.get("owned_token_shares")
    return (order["action"]=="SELL" and evidence.get("owned_position_open") is True
            and isinstance(expected,(int,float)) and expected>0 and balance+.000001>=expected)
