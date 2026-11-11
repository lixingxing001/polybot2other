"""Immediate Paper has separate evidence/ledger and never claims model fitting."""
from copy import deepcopy
from decimal import Decimal as D
import json
from pathlib import Path
import sqlite3
import threading
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from polybot2other.hengzhun.pilot import PilotEngine,HengzhunPilotService
from polybot2other.hengzhun.pilot_data import build_seed, PilotPrior, SPOT, TWAP
from polybot2other.hengzhun.model import ModelUnavailable
from test_paired_paper import market, books, rules
from test_paired_v2_service import Market,OneCycle


def prediction(at,up=.8):
    return {'decision_at':at,'source_at':at,'p_raw_up':up,'p_raw_down':1-up,'seed_manifest_sha256':'a'*64}


class PilotExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'pilot.sqlite3';self.e=PilotEngine(self.path,at_ms=0)
        self.addCleanup(self.e.close)

    def tick(self,at,*,up=.8,book=True,term=None):
        self.e.prediction=prediction(at,up);self.e.input_error=None
        self.e.tick(at,market(),books(at) if book else {},term or {**rules(),'checked_at':at})

    def filled(self):
        self.tick(210000);self.tick(211900);self.tick(212000)
        self.assertEqual(len(self.e.orders()),1)
        self.assertTrue(self.e.groups()[0]['first_fill'])

    def test_prior_can_enter_without_fake_trained_model(self):
        self.tick(210000)
        order=self.e.orders()[0]
        self.assertEqual(order['kind'],'ENTRY');self.assertEqual(order['side'],'Up')
        self.assertLessEqual(self.e.reserved(),D('5'))
        self.assertFalse(self.e.snapshot(210000)['live_enabled'])
        self.assertEqual(self.e.groups()[0]['experiment'],'UNCALIBRATED_EXISTING_DATA')

    def test_price_edge_and_time_thresholds_remain(self):
        self.tick(200000);self.assertEqual(self.e.orders(),[])
        self.tick(210000,up=.5);self.assertEqual(self.e.orders(),[])
        self.tick(271000);self.assertEqual(self.e.orders(),[])

    def test_stale_prior_cannot_trigger_entry(self):
        self.e.prediction=prediction(200000);self.e.input_error=None
        self.e.tick(210000,market(),books(210000),{**rules(),'checked_at':210000})
        self.assertEqual(self.e.orders(),[]);self.assertEqual(self.e.last_code,'PRICE_STALE')

    def test_one_intent_even_if_arrival_missing_and_after_restart(self):
        self.tick(210000);self.tick(212000,book=False)
        self.assertEqual(self.e.orders()[0]['filled'],'0')
        self.tick(214000);self.assertEqual(len(self.e.orders()),1)
        other=PilotEngine(self.path,at_ms=215000)
        try:
            other.prediction=prediction(216000);other.input_error=None
            other.tick(216000,market(),books(216000),{**rules(),'checked_at':216000})
            self.assertEqual(len(other.orders()),1)
        finally:other.close()

    def test_no_pairing_or_stop_order_and_confirm_then_official_settlement(self):
        self.filled();self.tick(230000)
        g=self.e.groups()[0]
        self.assertEqual(g['phase'],'HOLD_TO_FINAL');self.assertEqual(len(self.e.orders()),1)
        self.tick(301000)
        self.assertEqual(D(self.e.wallet()['realized']),0)
        self.e.official_result(g['id'],'Up',302000,{'synthetic':True});self.tick(302000)
        self.assertGreater(D(self.e.wallet()['realized']),0)
        before=self.e.wallet();self.tick(362000)
        self.assertEqual(self.e.wallet()['realized'],before['realized'])
        self.assertGreater(D(self.e.wallet()['cash']),100)

    def test_partial_fill_never_spends_more_than_budget(self):
        self.tick(210000)
        b=books(212000)
        for side in b:
            for level in b[side]['asks']:level['size']='2'
        self.e.tick(212000,market(),b,{**rules(),'checked_at':212000})
        g=self.e.groups()[0]
        self.assertGreater(D(g['spent']),0);self.assertLess(D(g['spent']),5)
        self.assertEqual(self.e.orders()[0]['status'],'DONE')
        self.assertEqual(D(self.e.wallet()['cash'])+sum(map(D,g['cost'].values())),100)

    def test_changed_fee_terms_abort_arrival_without_reposting(self):
        self.tick(210000)
        terms={**rules(),'checked_at':212000,'fee_rate':'.08'}
        self.e.tick(212000,market(),books(212000),terms)
        self.assertEqual(self.e.orders()[0]['filled'],'0');self.assertEqual(self.e.orders()[0]['status'],'DONE')

    def test_prior_loss_and_reservations_prevent_excess_risk(self):
        with self.e.tx():
            self.e.conn.execute("UPDATE pair_wallet SET cash='93',realized='-7'")
            self.e.conn.execute("INSERT INTO pair_profit(at,group_id,pnl,kind) VALUES(100000,'old','-7','TEST')")
        self.tick(210000)
        self.assertEqual(self.e.orders(),[]);self.assertEqual(self.e.last_code,'RISK_BUDGET')

    def test_pending_confirmation_survives_restart(self):
        self.filled();balance=self.e.wallet()['cash']
        other=PilotEngine(self.path,at_ms=213000)
        try:
            other.tick(225000,None,{},{})
            self.assertEqual(other.wallet()['cash'],balance)
            self.assertEqual(other.conn.execute("SELECT count(*) FROM pair_fills WHERE status='MATCHED'").fetchone()[0],0)
        finally:other.close()

    def test_pause_after_submit_does_not_cancel_known_arrival(self):
        self.tick(210000)
        self.e.tick(212000,market(),books(212000),{**rules(),'checked_at':212000},entry_pause_reason='test pause')
        self.assertGreater(D(self.e.groups()[0]['spent']),0)

    def test_empty_newer_book_never_falls_back_to_older_favorable_depth(self):
        self.tick(210000);self.tick(211500)
        empty=books(212000);empty['Up']['asks']=[]
        self.e.tick(212000,market(),empty,{**rules(),'checked_at':212000})
        self.assertEqual(self.e.orders()[0]['filled'],'0')

    def test_disconnect_after_submit_is_not_an_assumed_fill(self):
        self.tick(210000);self.e.gap(211000,'synthetic disconnect');self.tick(212000)
        self.assertEqual(self.e.orders()[0]['filled'],'0')

    def test_conflicting_result_cannot_release_claims_or_open_new_orders(self):
        self.filled();self.tick(225000)
        self.e.official_result('m','Up',302000,{'first':True})
        self.e.official_result('m','Down',303000,{'conflict':True})
        before=self.e.wallet()['cash'];self.tick(400000)
        self.assertEqual(self.e.wallet()['cash'],before)
        self.assertEqual(self.e.groups()[0]['phase'],'RESULT_CONFLICT')


class PilotDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.source=self.root/'source.sqlite3'
        c=sqlite3.connect(self.source)
        c.executescript('''CREATE TABLE price_observations(channel TEXT,source_at INTEGER,received_at INTEGER,value TEXT,event_id TEXT);
            CREATE TABLE market_contracts(json TEXT);CREATE TABLE official_results(json TEXT);
            CREATE TABLE landmarks(status TEXT);CREATE TABLE capture_meta(key TEXT,value TEXT);
            INSERT INTO capture_meta VALUES('run_id','original');INSERT INTO landmarks VALUES('MISSING_OR_TOO_LATE');''')
        rows=[]
        for t in range(1000,901000,1000):
            rows.extend([(SPOT,t,t+500,'100','s'+str(t)),(TWAP,t,t+600,'100','t'+str(t))])
        rows.append((SPOT,950000,960000,'999','future'))
        c.executemany('INSERT INTO price_observations VALUES(?,?,?,?,?)',rows);c.commit();c.close()
        self.seed=self.root/'seed';self.m=build_seed(self.source,self.seed,901000)

    def test_seed_uses_existing_rows_without_upgrading_missing_landmarks(self):
        self.assertEqual(self.m['price_rows'],1800)
        self.assertEqual(self.m['original_landmarks'],{'MISSING_OR_TOO_LATE':1})
        self.assertEqual(self.m['model_status'],'UNFITTED')
        self.assertGreater(self.m['proxy_sample_count'],0)
        with sqlite3.connect(self.source) as c:self.assertEqual(c.execute('select count(*) from price_observations').fetchone()[0],1801)

    def test_predictions_are_uncalibrated_and_reject_stale_prices(self):
        prior=PilotPrior(self.seed,901000)
        m={**market(),'target_price':100,'ends_at':960000}
        spot={'source_ms':900000,'received_ms':900500,'price':100}
        result=prior.predict(m,spot,901000)
        self.assertAlmostEqual(result['p_raw_up'],.5)
        self.assertFalse(result['proxy_calibrated'])
        with self.assertRaises(ModelUnavailable):prior.predict(m,spot,906000)

    def test_final_minute_does_not_fill_missing_integration_ticks(self):
        prior=PilotPrior(self.seed,901000);prior.observations={900000:prior.observations[900000]}
        m={**market(),'target_price':100,'ends_at':930000}
        with self.assertRaises(ModelUnavailable):prior.predict(m,{'source_ms':900000,'received_ms':900500,'price':100},901000)

    def test_seed_integrity_and_existing_seed_protection(self):
        with self.assertRaises(ValueError):PilotPrior(self.seed,900000)
        with self.assertRaises(ValueError):build_seed(self.source,self.seed,901000)
        m=json.loads((self.seed/'manifest.json').read_text());m['proxy_bias_bps']=100
        (self.seed/'manifest.json').write_text(json.dumps(m))
        with self.assertRaises(ValueError):PilotPrior(self.seed,901000)

    def test_future_source_and_receive_timestamps_are_not_consumed(self):
        p=PilotPrior(self.seed,901000);m={**market(),'target_price':100,'ends_at':960000}
        for s,r in [(902000,902000),(900000,902000)]:
            with self.assertRaises(ModelUnavailable):p.predict(m,{'source_ms':s,'received_ms':r,'price':100},901000)

    def test_service_cycle_reads_reference_and_submits_only_to_own_paper_ledger(self):
        connection=sqlite3.connect(':memory:')
        connection.execute('CREATE TABLE reference_ticks(channel TEXT,source_ms INTEGER,received_ms INTEGER,exact_price TEXT)')
        connection.executemany('INSERT INTO reference_ticks VALUES(?,?,?,?)',[(SPOT,t,t+500,'100') for t in range(1000,901000,1000)])
        reference=SimpleNamespace(conn=connection,lock=threading.RLock(),opening=lambda t:100)
        live=Market(started_at=690,ends_at=990)
        feed=SimpleNamespace(references=reference,books=SimpleNamespace(add_listener=Mock(),remove_listener=Mock()),
            observation=lambda:(live,{'state':'streaming','latest':{SPOT:{'price':100,'source_ms':900000,'received_ms':900500}}},books(901000,up='.30',down='.68'),901))
        with patch('polybot2other.hengzhun.pilot.now_ms',return_value=901000),patch('polybot2other.paired_paper_service.now_ms',return_value=901000):
            service=HengzhunPilotService(self.root/'paper',feed,seed=self.seed)
            try:
                service.rules['m']={**rules(),'checked_at':901000};service.stop_event=OneCycle()
                with patch('httpx.Client.post',side_effect=AssertionError('No live order allowed')):
                    service._run();snapshot=service.snapshot()
                self.assertIsNone(service.error)
                self.assertEqual(len(service.engine.orders()),1)
                self.assertTrue(snapshot['pilot']);self.assertEqual(snapshot['mode'],'PAPER')
                self.assertFalse(snapshot['formal_paper_qualified'])
                self.assertEqual(snapshot['model_status'],'UNFITTED')
                self.assertEqual(snapshot['wallet']['initial_cash'],'100')
                self.assertEqual(snapshot['seed']['price_rows'],1800)
                service.capture('B1_FRAME',{},901000)
                self.assertEqual(service.pending.qsize(),0)
                with self.assertRaises(ValueError):service.update_settings({'live_enabled':True})
            finally:service.engine.close();service.http.close();connection.close()
