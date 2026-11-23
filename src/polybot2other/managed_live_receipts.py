"""Exact six-decimal pUSD and conditional-token amounts from Polygon CTF Exchange V2."""
from __future__ import annotations

from decimal import Decimal
import re

import httpx
from eth_utils import keccak
from py_clob_client_v2.config import get_contract_config


POLYGON_CHAIN_ID = 137
POLYGON_RPCS = ("https://polygon-bor-rpc.publicnode.com", "https://polygon.drpc.org")
# Layout emitted by Polymarket/ctf-exchange-v2, src/exchange/mixins/Events.sol.
ORDER_FILLED = "0x" + keccak(
    text="OrderFilled(bytes32,address,address,uint8,uint256,uint256,uint256,uint256,bytes32,bytes32)"
).hex()
HASH = re.compile(r"0x[0-9a-fA-F]{64}")
ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
QUANTITY = re.compile(r"0x[0-9a-fA-F]+")
MICRO = Decimal(1_000_000)


class ReceiptPending(RuntimeError):
    """Temporary RPC or CLOB propagation delay. Never substitute estimated money."""


def read_polygon_receipt(http, tx_hash):
    """Both calls are reads. Verify Polygon mainnet before trusting the provider."""
    def rpc(url, method, params):
        response = http.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        response.raise_for_status()
        payload = response.json()
        return payload.get("result") if isinstance(payload, dict) and "error" not in payload else None

    for url in POLYGON_RPCS:
        try:
            if rpc(url, "eth_chainId", []) != "0x89":
                continue
            receipt = rpc(url, "eth_getTransactionReceipt", [tx_hash])
        except (httpx.HTTPError, ValueError):
            continue
        if isinstance(receipt, dict) and receipt.get("blockHash") and isinstance(receipt.get("logs"), list):
            if str(receipt.get("transactionHash", "")).lower() != tx_hash:
                raise ValueError("Polygon 回执交易哈希与官方成交不一致")
            return receipt
    raise ReceiptPending("Polygon 成交回执暂未就绪，继续核对")


def _quantity(raw, label):
    if not isinstance(raw, str) or not QUANTITY.fullmatch(raw):
        raise ValueError(f"Polygon 回执{label}无效")
    return int(raw, 16)


def _topic(address):
    return "0x" + address[2:].lower().rjust(64, "0")


def _amount(units):
    exact = Decimal(units) / MICRO
    result = float(exact)
    if Decimal(str(result)) != exact:
        raise ValueError("Polygon 整数金额超过账本可精确表示的范围")
    return result


def order_receipt_amounts(receipt, *, tx_hash, order_hash, owner, exchange, side, token_id):
    """Sum unique matching events. Ignore counterparty orders and FeeCharged fees."""
    if not (HASH.fullmatch(tx_hash) and HASH.fullmatch(order_hash)
            and ADDRESS.fullmatch(owner) and ADDRESS.fullmatch(exchange)):
        raise ValueError("缺少可验证的成交订单或钱包身份")
    if str(receipt.get("transactionHash", "")).lower() != tx_hash:
        raise ValueError("Polygon 回执交易哈希与官方成交不一致")
    if _quantity(receipt.get("status"), "状态") != 1:
        raise ValueError("官方已确认成交与 Polygon 交易执行状态不一致")
    block_hash = str(receipt.get("blockHash", "")).lower()
    if not HASH.fullmatch(block_hash):
        raise ValueError("Polygon 回执区块哈希无效")
    block_number = _quantity(receipt.get("blockNumber"), "区块号")
    if side not in {"BUY", "SELL"}:
        raise ValueError("缺少可验证的本地订单方向")
    if not str(token_id).isdecimal():
        raise ValueError("缺少可验证的本地代币编号")

    events = {}
    for log in receipt["logs"]:
        topics = log.get("topics") if isinstance(log, dict) else None
        if not isinstance(topics, list) or len(topics) < 2:
            continue
        if str(topics[0]).lower() != ORDER_FILLED or str(topics[1]).lower() != order_hash.lower():
            continue
        if str(log.get("address", "")).lower() != exchange.lower():
            continue
        if (len(topics) != 4 or str(topics[2]).lower() != _topic(owner)
                or str(topics[3]).lower() != _topic(exchange)):
            raise ValueError("Polygon 成交的钱包或撮合身份与本地订单不一致")
        if log.get("removed"):
            raise ReceiptPending("Polygon 成交区块正在重新确认")
        if (str(log.get("transactionHash", "")).lower() != tx_hash
                or str(log.get("blockHash", "")).lower() != block_hash
                or _quantity(log.get("blockNumber"), "日志区块号") != block_number):
            raise ValueError("Polygon 成交日志与交易回执不一致")
        data = log.get("data")
        if not isinstance(data, str) or not re.fullmatch(r"0x[0-9a-fA-F]{448}", data):
            raise ValueError("Polygon V2 成交事件长度或格式无效")
        log_side, token, making, taking, fee, _, _ = [
            int(data[i:i+64], 16) for i in range(2, len(data), 64)
        ]
        if log_side != (0 if side == "BUY" else 1) or token != int(token_id):
            raise ValueError("Polygon 成交方向或代币与本地订单不一致")
        shares, notional = (taking, making) if side == "BUY" else (making, taking)
        if not (0 < notional <= shares) or (side == "SELL" and fee > notional):
            raise ValueError("Polygon 成交金额、份额或费用无效")
        index = _quantity(log.get("logIndex"), "日志下标")
        event = (shares, notional, fee, data.lower())
        if index in events and events[index] != event:
            raise ValueError("同一 Polygon 成交日志重复且金额不一致")
        events[index] = event

    if not events:
        raise ValueError("Polygon 交易未包含匹配本订单的官方 V2 成交事件")
    shares = sum(event[0] for event in events.values())
    notional = sum(event[1] for event in events.values())
    fee = sum(event[2] for event in events.values())
    fill = {"trade_id": "polygon:" + tx_hash, "shares": _amount(shares),
            "price": float(Decimal(notional) / Decimal(shares)), "notional": _amount(notional),
            "fee": _amount(fee), "fee_source": "polygon_order_filled_v2"}
    details = {"block_hash": block_hash, "block_number": block_number, "log_indexes": sorted(events),
               "shares_units": shares, "notional_units": notional, "fee_units": fee}
    return fill, details


def confirmed_receipt_fills(http, order, info, trades, *, chain_id, funder):
    if chain_id != POLYGON_CHAIN_ID:
        raise ValueError("当前仅支持 Polygon 主网 V2 成交对账")
    contracts = get_contract_config(chain_id)
    exchange = contracts.neg_risk_exchange_v2 if info["neg_risk"] else contracts.exchange_v2
    groups = {}
    for trade in trades:
        tx_hash = str(trade.get("transaction_hash") or "").lower()
        if not tx_hash:
            raise ReceiptPending("官方成交尚未给出 Polygon 交易哈希，继续核对")
        if not HASH.fullmatch(tx_hash):
            raise ValueError("官方成交的 Polygon 交易哈希格式无效")
        groups.setdefault(tx_hash, []).append(trade)

    fills, associations, details = [], {}, {}
    for tx_hash, rows in sorted(groups.items()):
        receipt = read_polygon_receipt(http, tx_hash)
        fill, detail = order_receipt_amounts(
            receipt, tx_hash=tx_hash, order_hash=order["external_id"], owner=funder,
            exchange=exchange, side=order["action"], token_id=order["token_id"],
        )
        # Another CLOB row for this tx can arrive on a later poll. Never apportion
        # an aggregate event across unidentified trades at their rounded prices.
        reported_shares = sum(Decimal(str(row["size"])) for row in rows)
        if reported_shares * MICRO != detail["shares_units"]:
            raise ReceiptPending("Polygon 成交总份额与当前官方成交明细尚未对齐，继续核对")
        key = fill["trade_id"]
        fills.append(fill)
        associations[key] = sorted(row["id"] for row in rows)
        details[key] = detail
    return fills, associations, details
