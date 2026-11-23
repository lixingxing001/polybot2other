"""Synthetic public-chain receipts; no credentials, network or real orders."""
import copy
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from py_clob_client_v2.config import get_contract_config

from polybot2other.managed_live_client import ManagedExchange
from polybot2other.managed_live_receipts import (
    ORDER_FILLED, ReceiptPending, order_receipt_amounts, read_polygon_receipt,
)


OWNER = "0x" + "ab" * 20
ORDER = "0x" + "11" * 32
TX = "0x" + "22" * 32
TX2 = "0x" + "33" * 32
BLOCK = "0x" + "44" * 32
EXCHANGE = get_contract_config(137).exchange_v2
NEG_EXCHANGE = get_contract_config(137).neg_risk_exchange_v2
TOKEN = str(2**200 + 1)


def address_topic(address):
    return "0x" + address[2:].lower().zfill(64)


def event(index=3, shares=1_398_000_000, notional=9_520_000, fee=472_750, side="BUY",
          exchange=EXCHANGE, tx_hash=TX, order_hash=ORDER, owner=OWNER):
    making, taking = (notional, shares) if side=="BUY" else (shares, notional)
    data = [int(side=="SELL"), int(TOKEN), making, taking, fee, 0, 0]
    return {"logIndex":hex(index), "address": exchange, "transactionHash":tx_hash, "blockHash":BLOCK,
            "blockNumber":"0x123", "topics":[ORDER_FILLED,order_hash,address_topic(owner),address_topic(exchange)],
            "data":"0x"+"".join(f"{value:064x}" for value in data), "removed":False}


def receipt(*logs, tx=TX):
    return {"transactionHash":tx,"status":"0x1","blockHash":BLOCK,"blockNumber":"0x123","logs":list(logs)}


def amounts(value, side="BUY", exchange=EXCHANGE):
    return order_receipt_amounts(value, tx_hash=TX, order_hash=ORDER, owner=OWNER, side=side,
                                 token_id=TOKEN, exchange=exchange)


class ReceiptAmounts(unittest.TestCase):
    def test_exact_buy_ignores_other_orders_and_fee_logs(self):
        ours=event()
        counterparty=event(index=1, order_hash="0x"+"55"*32)
        charged={**ours, "logIndex":"0x2","topics":["0x"+"66"*32,ORDER]}
        fill, details=amounts(receipt(counterparty, charged, ours, copy.deepcopy(ours)))
        self.assertEqual(details["log_indexes"], [3])
        self.assertEqual((fill["shares"], fill["notional"], fill["fee"]), (1398,9.52,.47275))
        self.assertAlmostEqual(fill["price"],9.52/1398)
        self.assertAlmostEqual(fill["notional"]+fill["fee"],9.99275)

    def test_sell_on_negative_risk_contract(self):
        first=event(index=8,side="SELL",shares=4_000_000,notional=2_600_000,fee=75_000,exchange=NEG_EXCHANGE)
        second=event(index=9,side="SELL",shares=1_000_000,notional=630_003,fee=1,exchange=NEG_EXCHANGE)
        fill,details=amounts(receipt(second,first),side="SELL",exchange=NEG_EXCHANGE)
        self.assertEqual((fill["shares"],fill["notional"],fill["fee"]), (5,3.230003,.075001))
        self.assertEqual(details["log_indexes"],[8,9])

    def test_receipt_identity_status_and_data_cannot_be_substituted(self):
        ours=event()
        not_ours=event(owner="0x"+"99"*20)
        not_ours["topics"][3]=address_topic(OWNER)
        tests=[
            receipt(not_ours), receipt(event(exchange=NEG_EXCHANGE)), receipt(event(side="SELL")),
            receipt({**ours,"data":"0x"+"00"*224}), receipt({**ours,"data":ours["data"][:-2]}),
            receipt({**ours,"transactionHash":TX2}), receipt({**ours,"blockHash":TX2}),
            {**receipt(ours),"transactionHash":TX2},{**receipt(ours),"status":"0x0"},
            receipt(event(),event(fee=1)),
            receipt(event(order_hash=TX2)), receipt({**ours,"topics":ours["topics"][:3]}),
        ]
        for value in tests:
            with self.subTest(receipt=value), self.assertRaises(ValueError):amounts(value)
        with self.assertRaises(ReceiptPending):amounts(receipt({**ours,"removed":True}))

    def test_rpc_checks_chain_and_fails_over_without_posting_transactions(self):
        def result(payload):return httpx.Response(200,json={"result":payload},request=httpx.Request("POST","https://rpc.invalid"))
        http=Mock()
        http.post.side_effect=[result("0x1"), result("0x89"), result(receipt(event()))]
        self.assertEqual(read_polygon_receipt(http,TX)["transactionHash"],TX)
        self.assertEqual([c.kwargs["json"]["method"] for c in http.post.call_args_list],
                         ["eth_chainId","eth_chainId","eth_getTransactionReceipt"])
        for response in (httpx.ReadTimeout("busy"), result(None)):
            http.post.side_effect=[response,response]
            with self.assertRaises(ReceiptPending):read_polygon_receipt(http,TX)


class ExactReconciliation(unittest.TestCase):
    def setUp(self):
        self.exchange=ManagedExchange.__new__(ManagedExchange)
        self.exchange.settings=SimpleNamespace(live_trading_chain_id=137)
        self.exchange.http=Mock()
        self.client=Mock()
        self.exchange.client=Mock(return_value=({"TradeParams":lambda **x:x},self.client))
        self.order={"external_id":ORDER,"action":"BUY","token_id":TOKEN,"created_at":10}
        self.client.get_order.return_value={"id":ORDER,"status":"MATCHED","size_matched":"1398"}
        self.info={"neg_risk":False}
        self.market=SimpleNamespace(condition_id="condition")
        owner=patch.dict(os.environ,{"POLYBOT2OTHER_LIVE_FUNDER_ADDRESS":OWNER});owner.start();self.addCleanup(owner.stop)

    def clob(self,ident,shares,tx_hash=TX,status="CONFIRMED"):
        return {"id":ident,"taker_order_id":ORDER,"asset_id":TOKEN,"side":"BUY","status":status,
                "size":str(shares),"price":"0.007","transaction_hash":tx_hash}

    def reconcile(self,*rows):
        self.client.get_trades.return_value=list(rows)
        return self.exchange.reconcile(self.order,self.market,self.info)

    def test_shared_transaction_is_booked_once_at_exact_amounts(self):
        self.order["submission_json"]=json.dumps({"accepted":True,"status":"matched","trade_ids":["one","two"]})
        with patch("polybot2other.managed_live_receipts.read_polygon_receipt",return_value=receipt(event())) as read:
            result=self.reconcile(self.clob("one",1000),self.clob("two",398),self.clob("one",1000))
        read.assert_called_once_with(self.exchange.http,TX)
        self.assertTrue(result["terminal"])
        self.assertEqual(len(result["fills"]),1)
        fill=result["fills"][0]
        self.assertEqual((fill["trade_id"],fill["notional"],fill["fee"]),("polygon:"+TX,9.52,.47275))
        self.assertEqual(result["evidence"]["receipt_groups"],{"polygon:"+TX:["one","two"]})
        self.assertEqual(result["evidence"]["receipt_details"]["polygon:"+TX]["shares_units"],1_398_000_000)

    def test_missing_unavailable_or_partial_receipts_defer_full_batch(self):
        rows=[self.clob("one",1000),self.clob("two",398,TX2)]
        first=receipt(event(shares=1_000_000_000,notional=6_800_000))
        second=receipt(event(shares=398_000_000,notional=2_700_000,tx_hash=TX2),tx=TX2)
        reader="polybot2other.managed_live_receipts.read_polygon_receipt"
        with patch(reader,side_effect=[first,ReceiptPending("稍后"),first,second]):
            delayed=self.reconcile(*rows)
            self.assertEqual(delayed["fills"],[])
            self.assertFalse(delayed["no_fill"]);self.assertFalse(delayed["terminal"])
            result=self.reconcile(*reversed(rows))
            self.assertTrue(result["terminal"]);self.assertEqual(len(result["fills"]),2)
        with patch(reader,return_value=receipt(event())):
            partial=self.reconcile(self.clob("one",1000))
            self.assertFalse(partial["terminal"]);self.assertEqual(partial["fills"],[])
            self.assertEqual(partial["evidence"]["matched_trade_ids"],["one"])
        no_hash=self.reconcile(self.clob("one",1398,""))
        self.assertFalse(no_hash["terminal"]);self.assertEqual(no_hash["fills"],[])

    def test_ack_and_previous_partial_information_do_not_become_no_fill(self):
        self.order["submission_json"]=json.dumps({"accepted":True,"status":"MATCHED","trade_ids":["one","two"]})
        self.client.get_order.return_value.update(size_matched="0",status="CANCELED")
        with patch("polybot2other.managed_live_receipts.read_polygon_receipt") as read:
            for rows in ([],[self.clob("one",1,status="FAILED")],
                         [self.clob("one",1,status="FAILED"),self.clob("two",1,status="MINED")]):
                state=self.reconcile(*rows)
                self.assertFalse(state["terminal"]);self.assertFalse(state["no_fill"])
            no_fill=self.reconcile(self.clob("one",1,status="FAILED"),self.clob("two",1,status="FAILED"))
            self.assertTrue(no_fill["terminal"]);self.assertTrue(no_fill["no_fill"])
            self.assertEqual(no_fill["evidence"]["failed_trade_ids"],["one","two"])
            read.assert_not_called()
