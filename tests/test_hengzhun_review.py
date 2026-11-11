"""Remaining original acceptance variants, using the existing synthetic fixtures."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

import test_hengzhun_execution as f
from polybot2other.hengzhun.ledger import HengzhunLedger,LedgerError
from polybot2other.hengzhun.execution import PaperVenue
from polybot2other.hengzhun.contracts import contract_hash


class AcceptanceReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.ledger=HengzhunLedger(Path(self.tmp.name)/'ledger.sqlite3',dict(f.SPACE));self.addCleanup(self.ledger.close)
        self.ledger.ensure_wallet('W1');self.venue=PaperVenue(self.ledger,f.fee_adapter)

    def test_ex039_full_cost_and_claim_count_are_independently_reported(self):
        for market in ('one','two'):
            ident=self.ledger.reserve(f.plan(market),240000)['intent_id'];self.ledger.submit_once(ident,240000)
            self.ledger.record_match(ident,market+'-fill','.5',f.cost(),241000)
            self.ledger.confirm_fill(market+'-fill',253000);self.ledger.order_terminal(ident,253000,reconciled=True)
            self.ledger.record_result(f.result(market),310000)
        before=self.ledger.snapshot('W1',310000)
        self.assertEqual(before['claim_cost_committed'],'10');self.assertEqual(before['claim_pending_markets'],2)
        reasons=self.ledger.guard_reasons(f.plan('three'),310001)
        self.assertIn('COMMITTED_COST_LIMIT',reasons);self.assertIn('CLAIM_SLOT_LIMIT',reasons)
        with self.assertRaises(LedgerError):self.ledger.reserve(f.plan('three'),310001)
        self.assertEqual(self.ledger.snapshot('W1',310000),before)

    def test_ex012_fee_metadata_change_aborts_without_spend_or_new_eligibility(self):
        plan=f.plan();ident=self.ledger.reserve(plan,240000)['intent_id'];changed=f.contract()
        changed['trading']['fee_contract']['fee_model_id']='new-verified-fee-version'
        changed['contract_sha256']=contract_hash(changed)
        with self.assertRaisesRegex(LedgerError,'CONTRACT_CHANGED_BEFORE_SEND'):
            self.venue.submit(ident,changed,240000,clock_bound_ms=0)
        order=self.ledger.intent(ident)
        self.assertEqual(order['send_attempts'],0);self.assertEqual(order['reserved'],'0')
        self.assertEqual(order['state'],'ABORTED_BEFORE_SEND')
        self.assertEqual(self.ledger.snapshot('W1',240000)['cash_confirmed'],'100')
        self.assertTrue(any(e['kind']=='RELEASE_UNUSED_RESERVATION' for e in self.ledger.events()))
        with self.assertRaisesRegex(LedgerError,'MARKET_INTENT_CONSUMED'):self.ledger.reserve(plan,240001)

    def test_ex038_ex040_claim_keeps_verified_nav_and_realized_profit_unchanged(self):
        ident=self.ledger.reserve(f.plan(),240000)['intent_id'];self.ledger.submit_once(ident,240000)
        self.ledger.record_match(ident,'fill','.5',f.cost('8'),241000)
        self.ledger.confirm_fill('fill',253000);self.ledger.order_terminal(ident,253000,reconciled=True)
        self.ledger.record_result(f.result(),310000)
        before=self.ledger.snapshot('W1',310000)
        self.assertEqual((before['cash_confirmed'],before['claim_receivable'],before['nav'],before['claim_cost_committed']),('96','8','104','4'))
        self.ledger.claim(ident,'receipt','8',370000)
        self.ledger.claim(ident,'receipt','8',370000)
        after=self.ledger.snapshot('W1',370000)
        self.assertEqual((after['cash_confirmed'],after['claim_receivable'],after['nav'],after['claim_cost_committed']),('104','0','104','0'))
        self.assertEqual(before['realized_pnl'],after['realized_pnl'])
        self.assertEqual(after['claim_pending_markets'],0)


if __name__=='__main__':unittest.main()
