"""No funded orders: refusal recovery requires durable receipts and fresh reads."""
from copy import deepcopy
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
import httpx

from py_clob_client_v2.exceptions import PolyApiException
from polybot2other.managed_live_client import ManagedExchange
from polybot2other.managed_live_recovery import FAK_NO_MATCH_FULL
import test_managed_live as fixtures


class NoMatchRecoveryTests(unittest.TestCase):
    setUp=fixtures.ManagedLiveTests.setUp
    enable=fixtures.ManagedLiveTests.enable
    buy=fixtures.ManagedLiveTests.buy
    confirm=fixtures.ManagedLiveTests.confirm

    def missed(self):
        self.exchange.fail_post=True;self.enable();self.manager.tick()
        order=self.store.active_orders()[0]
        self.store.record_submission(order['id'],{'status':'UNKNOWN','http_status':400,
            'error_message':FAK_NO_MATCH_FULL+'.','response_type':'dict','error_type':'PolyApiException'})
        self.store.quarantine_order(order['id'])
        self.proof={'order':'missing','trades':'checked','matching_trades':0,'pending_trades':False,
            'open_orders':'checked','matching_open_order':False,'token_balance':0.,
            'wallet_activity':'unavailable','checks_started_at':time.time()-1,'checks_completed_at':time.time()}
        return self.store.order(order['id'])

    def test_known_no_match_releases_without_waiting_for_market_expiry_or_activity_api(self):
        order=self.missed();before=self.store.config()
        self.assertEqual(self.manager.available_cash(),95)
        self.exchange.reconcile=Mock(return_value={'fills':[],'terminal':False,'no_fill':False,'evidence':self.proof})
        submitted=len(self.exchange.submitted)
        self.manager.reconcile()
        result=self.store.order(order['id'])
        self.assertEqual(result['state'],'REJECTED')
        self.assertEqual(self.manager.available_cash(),100)
        self.assertEqual(self.store.risk_usage('MAIN')['slots'],0)
        self.assertEqual(self.store.positions(),[])
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM fills').fetchone()[0],0)
        self.assertEqual(len(self.exchange.submitted),submitted)
        self.assertEqual(self.store.config(),before)
        self.assertEqual(json.loads(result['finalization_json'])['policy'],'official-fak-no-match-v1')
        self.assertFalse(self.store.recover_fak_no_match(order['id'],self.proof))

    def test_incomplete_stale_or_positive_evidence_cannot_release(self):
        order=self.missed()
        for change in ({'order':'found'},{'order':'unavailable'},{'matching_trades':1},{'pending_trades':True},
                       {'trades':'unavailable'},{'open_orders':'unavailable'},{'matching_open_order':True},
                       {'token_balance':None},{'token_balance':1.},{'token_balance':float('nan')},
                       {'checks_started_at':time.time()-31},{'checks_completed_at':time.time()+10},
                       {'confirmed_fills':[{'shares':1}]}):
            with self.subTest(change=change):
                self.assertFalse(self.store.recover_fak_no_match(order['id'],{**self.proof,**change}))
                self.assertEqual(self.manager.available_cash(),95)

    def test_ack_fill_or_fault_cannot_be_overridden_by_no_match_text(self):
        order=self.missed()
        self.store.record_ack(order['id'],order['external_id'])
        self.assertFalse(self.store.recover_fak_no_match(order['id'],self.proof))
        with self.store.conn:self.store.conn.execute('UPDATE orders SET ack_id=NULL WHERE id=?',(order['id'],))
        self.store.record_reconciliation_fault(order['id'],'requires review')
        self.assertFalse(self.store.recover_fak_no_match(order['id'],self.proof))
        self.assertEqual(self.manager.available_cash(),95)

    def test_timeout_generic_error_or_conflicting_receipt_never_released_as_no_match(self):
        order=self.missed()
        receipts=[{'status':'UNKNOWN','http_status':500,'error_message':FAK_NO_MATCH_FULL},
                  {'status':'UNKNOWN','http_status':400,'error_message':'other error'},
                  {'status':'UNKNOWN','http_status':400,'error_message':FAK_NO_MATCH_FULL,'accepted':True},
                  {'status':'MATCHED','http_status':400,'error_message':FAK_NO_MATCH_FULL},
                  {'status':'UNKNOWN','http_status':400,'error_message':FAK_NO_MATCH_FULL,'trade_ids':['trade']},
                  {'status':'UNKNOWN','http_status':400,'error_message':FAK_NO_MATCH_FULL,'response_acceptance_evidence':True}]
        for receipt in receipts:
            with self.subTest(receipt=receipt):
                self.store.record_submission(order['id'],receipt)
                self.assertFalse(self.store.recover_fak_no_match(order['id'],self.proof))

    def test_documented_error_with_attempt_identifier_is_refused_immediately(self):
        e=ManagedExchange(self.settings);self.addCleanup(e.http.close)
        sdk=e.legacy._sdk();client=Mock()
        body={'id':'rejected-attempt','error':FAK_NO_MATCH_FULL+'.'}
        client.post_order.side_effect=PolyApiException(httpx.Response(400,json=body))
        with patch.object(e,'client',return_value=(sdk,client)):
            result=e.submit(SimpleNamespace(signed=object(),external_id='0x'+'a'*64))
        self.assertTrue(result['rejected']);self.assertEqual(result['error_code'],'FAK_NO_MATCH')
        self.assertIsNone(result['external_id']);client.post_order.assert_called_once()

    def test_conflicting_success_or_fill_response_remains_uncertain(self):
        for extra in ({'success':True},{'status':'MATCHED'},{'tradeIDs':['trade']},{'makingAmount':'1'},
                      {'takingAmount':'NaN'},{'details':{'accepted':True}}):
            with self.subTest(extra=extra):
                self.assertIsNone(ManagedExchange._definite_rejection({'error':FAK_NO_MATCH_FULL,**extra},400))
        nested={'error':json.dumps({'message':FAK_NO_MATCH_FULL,'tradeIDs':['trade']})}
        self.assertIsNone(ManagedExchange._definite_rejection(nested,400))

    def test_display_prices_separate_fees_partial_sales_and_official_settlement(self):
        position=self.buy(shares=10,price=.4)
        self.exchange.bid=.6;self.exchange.ask=.61
        order=self.manager.close_position('MAIN',position['id'])
        self.confirm(order,4,.6,trade_id='partial-sale')
        before=self.store.position(position['id'])
        row=next(v for v in self.store.snapshot()['variants'] if v['variant_id']=='MAIN')['recent_trades'][0]
        self.assertAlmostEqual(row['entry_price'],.4)
        self.assertAlmostEqual(row['sell_price'],.6)
        self.assertEqual(row['sold_shares'],4)
        self.assertIsNone(row['settlement_price'])
        self.assertEqual(self.store.position(position['id']),before)
        self.store.resolve(self.market.round_id,'Up')
        row=next(v for v in self.store.snapshot()['variants'] if v['variant_id']=='MAIN')['recent_trades'][0]
        self.assertAlmostEqual(row['entry_price'],.4)
        self.assertAlmostEqual(row['sell_price'],.6)
        self.assertEqual(row['settlement_price'],1.)


if __name__=='__main__':unittest.main()
