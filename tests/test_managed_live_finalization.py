"""Expired-order evidence and refusal diagnostics; no real orders or credentials."""
from copy import deepcopy
from dataclasses import replace
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch

import httpx
from py_clob_client_v2.exceptions import PolyApiException
from polybot2other.managed_live_client import ManagedExchange,OrderSubmissionUncertain
from polybot2other.managed_live_recovery import normalize_error,LATE_AUDIT_FAULT
from polybot2other.managed_live import ManagedLiveAccounts
import test_managed_live as fixtures


class FinalizationTests(unittest.TestCase):
    setUp=fixtures.ManagedLiveTests.setUp
    enable=fixtures.ManagedLiveTests.enable
    buy=fixtures.ManagedLiveTests.buy
    confirm=fixtures.ManagedLiveTests.confirm

    def healthy_clock(self,now):
        if not hasattr(self,"clock"):
            clock=patch("time.time",return_value=now);self.clock=clock.start();self.addCleanup(clock.stop)
        else:self.clock.return_value=now
        self.manager.wallet.update(ready=True,checked_at=now,read_started_at=now)
        self.manager.geo.update(ready=True,blocked=False,checked_at=now)
        self.manager.open_orders_at=now

    def missing_expired(self,action="BUY"):
        if action=="SELL":
            position=self.buy();self.exchange.fail_post=True
            order=self.manager.close_position("MAIN",position["id"])
        else:
            self.exchange.fail_post=True;self.enable();self.manager.tick();order=self.store.active_orders()[0]
        self.store.quarantine_order(order["id"])
        self.healthy_clock(self.market.ends_at+601)
        self.feed.polymarket.get_resolution.return_value={"outcome":"Down"}
        self.proof={"order":"missing","trades":"checked","matching_trades":0,"pending_trades":False,
                    "open_orders":"checked","matching_open_order":False,"token_balance":10 if action=="SELL" else 0,
                    "wallet_activity":"checked","wallet_activity_count":0,"wallet_activity_complete":True}
        self.exchange.reconcile=Mock(side_effect=lambda *args:{"fills":[],"terminal":False,"no_fill":False,
                                      "pending_reason":"等待多源核对","evidence":{**deepcopy(self.proof),"checks_started_at":time.time()-getattr(self,"snapshot_age",0),"checks_completed_at":time.time()}})
        return order

    def probes(self,count=3):
        for _ in range(count):
            self.manager.reconcile();self.healthy_clock(time.time()+16)

    def test_three_independent_checks_release_budget_and_slots_without_fills(self):
        order=self.missing_expired();self.probes(2)
        self.assertEqual(self.store.order(order["id"])["state"],"QUARANTINED")
        self.assertEqual(self.manager.available_cash(),95)
        self.probes(1);finished=self.store.order(order["id"])
        self.assertEqual(finished["state"],"EXPIRED_UNFILLED")
        self.assertEqual(self.manager.available_cash(),100)
        self.assertEqual(self.store.risk_usage("MAIN")["slots"],0)
        self.assertEqual(self.store.positions(),[])
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM fills').fetchone()[0],0)
        proof=json.loads(finished["finalization_json"])
        self.assertEqual(proof["consecutive_checks"],3)
        self.assertGreaterEqual(proof["last_check_at"]-proof["first_check_at"],30)

    def test_failed_sell_finalizes_then_records_real_official_settlement(self):
        order=self.missing_expired("SELL");self.probes()
        self.assertEqual(self.store.order(order["id"])["state"],"EXPIRED_UNFILLED")
        position=self.store.positions()[0]
        self.assertEqual(position["status"],"SETTLED")
        self.assertAlmostEqual(position["realized_pnl"],-4.168)
        self.assertEqual(self.manager.wallet["balance"],100)
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM fills").fetchone()[0],1)

    def test_incomplete_or_conflicting_source_never_closes(self):
        order=self.missing_expired()
        for field,value in [("order","unavailable"),("order","incomplete"),("trades","unavailable"),
                            ("matching_trades",1),("pending_trades",True),("open_orders","unavailable"),
                            ("matching_open_order",True),("wallet_activity","unavailable"),("wallet_activity_count",1),
                            ("wallet_activity_complete",False),("token_balance",1),("token_balance",None)]:
            original=self.proof[field];self.proof[field]=value
            with self.subTest(field=field,value=value):
                self.probes();self.assertEqual(self.store.order(order["id"])["state"],"QUARANTINED")
                self.assertEqual(self.store.order(order["id"])["closure_checks"],0)
            self.proof[field]=original

    def test_failure_resets_consecutive_checks(self):
        order=self.missing_expired();self.probes(2)
        self.proof["wallet_activity"]="unavailable";self.probes(1)
        self.proof["wallet_activity"]="checked";self.probes(2)
        self.assertEqual(self.store.order(order["id"])["state"],"QUARANTINED")
        self.probes(1);self.assertEqual(self.store.order(order["id"])["state"],"EXPIRED_UNFILLED")

    def test_old_evidence_cannot_release_a_current_reservation(self):
        order=self.missing_expired();self.snapshot_age=40
        self.probes();self.assertEqual(self.store.order(order["id"])["closure_checks"],0)
        self.snapshot_age=0;self.probes()
        self.assertEqual(self.store.order(order["id"])["state"],"EXPIRED_UNFILLED")

    def test_elapsed_time_or_resolution_alone_never_closes(self):
        order=self.missing_expired();self.healthy_clock(self.market.ends_at+500)
        self.probes();self.assertEqual(self.store.order(order["id"])["closure_checks"],0)
        self.healthy_clock(self.market.ends_at+601);self.feed.polymarket.get_resolution.return_value=None
        self.probes();self.assertEqual(self.store.order(order["id"])["closure_checks"],0)
        self.feed.polymarket.get_resolution.return_value={"outcome":"Down"};self.manager.wallet["ready"]=False
        self.manager.reconcile();self.assertEqual(self.store.order(order["id"])["closure_checks"],0)

    def test_acknowledged_order_or_real_partial_fill_not_closed_as_empty(self):
        order=self.missing_expired();self.store.record_ack(order["id"],order["external_id"])
        self.probes();self.assertEqual(self.store.order(order["id"])["state"],"QUARANTINED")
        with self.store.conn:self.store.conn.execute("UPDATE orders SET ack_id=NULL WHERE id=?",(order["id"],))
        self.store.apply_confirmed_fill(order["id"],"real-part",1,.4,.0168,"official_market_fee_schedule")
        self.probes();self.assertEqual(self.store.order(order["id"])["state"],"QUARANTINED")

    def test_sell_requires_all_known_token_holdings_still_present(self):
        order=self.missing_expired("SELL");self.proof["token_balance"]=5
        self.probes();self.assertEqual(self.store.order(order["id"])["state"],"QUARANTINED")

    def test_audit_read_failure_does_not_reopen_but_late_evidence_does(self):
        order=self.missing_expired();self.probes();proof=self.store.order(order["id"])["finalization_json"]
        self.healthy_clock(time.time()+301);self.exchange.reconcile.side_effect=TimeoutError
        self.manager.reconcile();self.assertEqual(self.store.order(order["id"])["state"],"EXPIRED_UNFILLED")
        self.healthy_clock(time.time()+301);self.exchange.reconcile.side_effect=None
        fill={"trade_id":"late-proof","shares":2,"price":.4,"fee":.0336,"fee_source":"official_market_fee_schedule"}
        self.exchange.reconcile.return_value={"fills":[fill],"terminal":True,"no_fill":False,"evidence":{"matching_trades":1}}
        self.manager.reconcile();reopened=self.store.order(order["id"])
        self.assertEqual(reopened["state"],"UNKNOWN");self.assertIn("出现新的",self.manager._fault)
        self.assertEqual(reopened["finalization_json"],proof)
        self.assertEqual(json.loads(reopened["verification_json"])["confirmed_fills"],[fill])
        self.assertEqual(self.manager.available_cash(),95)
        self.healthy_clock(time.time()+301)
        self.exchange.reconcile.return_value={"fills":[],"terminal":False,"no_fill":False,"evidence":{**self.proof,"wallet_activity_count":1}}
        self.manager.reconcile();self.assertIsNotNone(self.manager._fault)

    def finished_sell(self):
        order=self.missing_expired('SELL');self.probes();self.healthy_clock(time.time()+301)
        return self.store.order(order['id'])

    def redemption_row(self,order):
        return {'proxyWallet':'0x'+'a'*40,'conditionId':self.market.condition_id,'timestamp':time.time(),
                'type':'REDEEM','asset':order['token_id'],'size':10,'usdcSize':0,'transactionHash':'0xredeem','outcome':'Up'}

    def settlement_evidence(self):
        return {**self.proof,'token_balance':0,'wallet_activity_count':0,'wallet_activity_complete':True,
                'wallet_settlement_count':1,'wallet_settlement_shares':10,'checks_started_at':time.time(),'checks_completed_at':time.time()}

    def test_post_settlement_redemption_is_recorded_without_reopening_or_stopping(self):
        order=self.finished_sell();e=ManagedExchange(self.settings);self.addCleanup(e.http.close)
        row=self.redemption_row(order);page=Mock();page.json.return_value=[row];empty=Mock();empty.json.return_value=[]
        with patch.dict('os.environ',{'POLYBOT2OTHER_LIVE_FUNDER_ADDRESS':row['proxyWallet']}),patch.object(e.http,'get',side_effect=[page,empty]):
            activity=e.wallet_activity_evidence(order,self.market)
        self.assertEqual(activity['wallet_activity_count'],0);self.assertEqual(activity['wallet_settlement_shares'],10)
        self.assertTrue(activity['wallet_activity_complete']);self.assertEqual(activity['wallet_activity_pages'],2)
        self.proof.update(activity,token_balance=0);self.manager.reconcile()
        self.assertEqual(self.store.order(order['id'])['state'],'EXPIRED_UNFILLED')
        self.assertIsNone(self.manager._fault);self.assertEqual(self.store.positions()[0]['realized_pnl'],-4.168)

    def test_redemption_cannot_hide_a_later_page_trade(self):
        order=self.finished_sell();e=ManagedExchange(self.settings);self.addCleanup(e.http.close)
        row=self.redemption_row(order);first=Mock();first.json.return_value=[row]
        second=Mock();second.json.return_value=[{**row,'type':'TRADE','side':'SELL','transactionHash':'0xtrade'}]
        with patch.dict('os.environ',{'POLYBOT2OTHER_LIVE_FUNDER_ADDRESS':row['proxyWallet']}),patch.object(e.http,'get',side_effect=[first,second]):
            result=e.wallet_activity_evidence(order,self.market)
        self.assertEqual(result['wallet_activity_count'],1);self.assertEqual(result['wallet_settlement_count'],1)
        self.assertFalse(result['wallet_activity_complete'])

    def test_redemption_exception_is_limited_to_proven_sell_holdings(self):
        order=self.finished_sell();e=ManagedExchange(self.settings);self.addCleanup(e.http.close);row=self.redemption_row(order)
        variants=[({**order,'action':'BUY'},row),({**order,'finalization_json':None},row),
                  (order,{**row,'timestamp':order['finalized_at']-1}),(order,{**row,'size':11}),
                  (order,{**row,'usdcSize':1}),(order,{**row,'asset':''}),(order,{**row,'type':'MERGE'})]
        for candidate,activity in variants:
            page=Mock();page.json.return_value=[activity]
            with self.subTest(activity=activity,action=candidate['action']),patch.dict('os.environ',{'POLYBOT2OTHER_LIVE_FUNDER_ADDRESS':row['proxyWallet']}),patch.object(e.http,'get',return_value=page):
                self.assertEqual(e.wallet_activity_evidence(candidate,self.market)['wallet_activity_count'],1)

    def test_unattributed_activity_is_scoped_and_keeps_budget_without_global_halt(self):
        order=self.missing_expired();self.probes();self.healthy_clock(time.time()+301)
        self.proof.update(wallet_activity_count=1,wallet_activity_complete=False)
        self.manager.reconcile()
        self.assertEqual(self.store.order(order['id'])['state'],'QUARANTINED')
        self.assertEqual(self.manager.available_cash(),95);self.assertEqual(self.store.risk_usage('MAIN')['slots'],1)
        self.assertIsNone(self.manager._access_block(require_wallet=True))

    def legacy_redemption_fault(self):
        order=self.finished_sell();old={**self.proof,'token_balance':0,'wallet_activity_count':1,'wallet_activity_complete':False}
        self.store.record_verification(order['id'],old);self.manager._record_reconciliation_fault(order,LATE_AUDIT_FAULT)
        self.proof=self.settlement_evidence()
        return order,old

    def test_persisted_false_alarm_recovers_on_restart_and_keeps_audit_and_finances(self):
        order,old=self.legacy_redemption_fault();before=self.store.config();position=deepcopy(self.store.positions()[0]);submits=len(self.exchange.submitted)
        restarted=ManagedLiveAccounts(self.settings,exchange=self.exchange,feed=self.feed,store=self.store)
        self.assertEqual(restarted._fault,LATE_AUDIT_FAULT);restarted.reconcile()
        recovered=self.store.order(order['id']);self.assertEqual(recovered['state'],'EXPIRED_UNFILLED')
        self.assertIsNone(restarted._fault);self.assertEqual(self.store.reconciliation_faults(),{})
        record=json.loads(recovered['audit_recovery_json'])[-1]
        self.assertEqual(record['previous_verification'],old);self.assertEqual(record['previous_fault']['message'],LATE_AUDIT_FAULT)
        self.assertEqual(self.store.config(),before);self.assertEqual(self.store.positions()[0],position)
        self.assertEqual(len(self.exchange.submitted),submits)

    def test_recovery_read_failure_preserves_original_evidence_for_automatic_retry(self):
        order,old=self.legacy_redemption_fault();saved=self.store.order(order['id'])['verification_json']
        self.assertTrue(self.manager._fault_recovers_automatically())
        self.assertEqual(self.manager.snapshot()['variants'][0]['execution']['recovery'],'auto')
        reconcile=self.exchange.reconcile.side_effect;self.exchange.reconcile.side_effect=TimeoutError
        self.manager.reconcile();self.assertEqual(self.store.order(order['id'])['verification_json'],saved)
        self.healthy_clock(time.time()+16);self.exchange.reconcile.side_effect=reconcile;self.manager.reconcile()
        self.assertEqual(self.store.order(order['id'])['state'],'EXPIRED_UNFILLED');self.assertIsNone(self.manager._fault)

    def test_false_alarm_recovery_does_not_clear_another_integrity_fault(self):
        order,old=self.legacy_redemption_fault()
        future=replace(self.market,round_id='other-live-market',started_at=time.time(),ends_at=time.time()+300)
        other=self.store.prepare_order('0x'+'d'*64,'MAIN',future,'up','Up','BUY',.5,cash=5)
        self.manager._record_reconciliation_fault(other,'成交的资产与本地订单不一致')
        self.manager.reconcile()
        self.assertEqual(self.store.order(order['id'])['state'],'EXPIRED_UNFILLED')
        self.assertIn('资产',self.manager._fault);self.assertIn(other['id'],self.store.reconciliation_faults())
        self.assertFalse(self.manager._fault_recovers_automatically())

    def test_activity_pagination_cannot_assume_short_page_is_complete(self):
        e=ManagedExchange(self.settings);self.addCleanup(e.http.close)
        funder="0x"+"a"*40;now=time.time();order={"created_at":now,"token_id":"up"}
        other={"proxyWallet":funder,"conditionId":self.market.condition_id,"timestamp":now,"type":"TRADE","asset":"other"}
        relevant={**other,"asset":"up"};first=Mock();first.json.return_value=[other];second=Mock();second.json.return_value=[relevant]
        with patch.dict("os.environ",{"POLYBOT2OTHER_LIVE_FUNDER_ADDRESS":funder}),patch.object(e.http,"get",side_effect=[first,second]) as get:
            result=e.wallet_activity_evidence(order,self.market)
        self.assertEqual(result["wallet_activity_count"],1);self.assertFalse(result["wallet_activity_complete"])
        self.assertEqual(get.call_args_list[1].kwargs["params"]["offset"],1)

    def test_activity_error_wrong_wallet_and_nonadvancing_pages_fail_closed(self):
        e=ManagedExchange(self.settings);self.addCleanup(e.http.close)
        funder="0x"+"a"*40;order={"created_at":time.time(),"token_id":"up"}
        row={"proxyWallet":funder,"conditionId":self.market.condition_id,"timestamp":time.time(),"type":"TRADE","asset":"other"}
        for body in ({"error":"unavailable"},[{**row,"proxyWallet":"another-wallet"}],[{**row,"timestamp":float('nan')}],[row]):
            response=Mock();response.json.return_value=body
            with self.subTest(body=body),patch.dict("os.environ",{"POLYBOT2OTHER_LIVE_FUNDER_ADDRESS":funder}),patch.object(e.http,"get",return_value=response),self.assertRaises(ValueError):
                e.wallet_activity_evidence(order,self.market)

    def test_text_json_nested_rejections_and_sanitized_unknown_diagnostics(self):
        for raw in ("no orders found to match with FAK order",json.dumps({"error":"no orders found to match with FAK order"}),
                    {"error":{"code":"BAD_REQUEST","message":"no orders found to match with FAK order"}}):
            with self.subTest(raw=raw):self.assertTrue(ManagedExchange._definite_rejection(raw,400)["rejected"])
        e=ManagedExchange(self.settings);self.addCleanup(e.http.close);sdk=e.legacy._sdk();client=Mock()
        secret="test-private-credential";body={"error":{"code":"UNKNOWN_NEW_CODE","message":f"new error; api_key={secret}; signature=0x"+'f'*130}}
        client.post_order.side_effect=PolyApiException(httpx.Response(400,json=body))
        self.manager.exchange=e;order=self.store.prepare_order("0x"+'c'*64,"MAIN",self.market,"up","Up","BUY",.5,cash=5)
        with patch.dict("os.environ",{"POLYBOT2OTHER_LIVE_API_KEY":secret}),patch.object(e,"client",return_value=(sdk,client)):
            self.manager._submit(order,SimpleNamespace(external_id=order["external_id"],signed=object()))
        stored=self.store.order(order["id"]);detail=json.loads(stored["submission_json"])
        self.assertEqual(stored["state"],"UNKNOWN");self.assertEqual(detail["http_status"],400)
        self.assertIn("new error",detail["error_message"]);self.assertNotIn(secret,stored["submission_json"])
        self.assertNotIn('f'*130,stored["submission_json"]);self.assertEqual(detail["error_code"],"UNKNOWN_NEW_CODE")
        client.post_order.assert_called_once()

    def test_unrecognized_error_structure_retains_diagnostic_fields_without_credentials(self):
        result=normalize_error({"issues":[{"field":"size","msg":"too small","apiKey":"not-in-current-env","request":{"privateKey":"hidden"}}]})
        self.assertIn("too small",result["error_message"])
        self.assertNotIn("not-in-current-env",result["error_message"])
        self.assertNotIn("hidden",result["error_message"])


if __name__=="__main__":unittest.main()
