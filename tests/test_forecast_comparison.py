"""Compare the same complete Paper markets across two isolated SQLite ledgers."""
from pathlib import Path
import tempfile
import unittest

from polybot2other.forecast_comparison import compare_ledgers
from polybot2other.forecast_observations import ForecastObservations
from polybot2other.models import MarketRound
from polybot2other.storage import TradeStore


class ForecastComparisonTests(unittest.TestCase):
    start = 1_794_982_800

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = self.create_store(Path(tmp.name)/"base.sqlite3", self.start-900)
        self.early = self.create_store(Path(tmp.name)/"early.sqlite3", self.start-5)

    def create_store(self, path, run_at):
        store = TradeStore(path, 100)
        self.addCleanup(store.conn.close)
        ForecastObservations(store)
        with store.conn:
            store.conn.executescript("""
                CREATE TABLE paper_sim_run(id INTEGER PRIMARY KEY, started_at REAL);
                CREATE TABLE paper_sim_orders(order_id INTEGER PRIMARY KEY, phase TEXT);
            """)
            store.conn.execute("INSERT INTO paper_sim_run VALUES(1,?)", (run_at,))
        return store

    def round(self, store, slot, missing=None, late=None):
        start = self.start+slot*300
        key = f"btc-updown-5m-{start}"
        store.upsert_round(MarketRound(key, "BTC", start, start+300, 60000))
        points = [180, 60, 20] if store is self.base else [240, 180, 60, 20]
        with store.conn:
            for s in points:
                due = start+300-s
                observed = None if s == missing else due+1 if s == late else due-1
                store.conn.execute("""INSERT INTO forecast_checkpoints(round_id,seconds_left,due_at,observed_at)
                    VALUES(?,?,?,?)""", (key,s,due,observed))
        return key

    def trade(self, store, key, left, status, pnl=None):
        end = store.get_round(key)["ends_at"]
        with store.conn:
            store.conn.execute("""INSERT INTO trades(round_id,symbol,side,stake,entry_price,shares,
                confidence,move_bps,status,opened_at,settled_at,pnl,reason)
                VALUES(?,'BTC','Up',1,.5,2,.7,0,?,?,?,?, 'test')""",
                (key, status, end-left, (end if status == "SETTLED" else None), pnl))

    def order(self, store, key, phase, left, action="FAK"):
        at = store.get_round(key)["ends_at"]-left
        with store.conn:
            order = store.conn.execute("""INSERT INTO paper_orders(round_id,symbol,side,order_type,status,
              requested_cash,reason,created_at,updated_at) VALUES(?,'BTC','Up',?,?,5,'test',?,?)""",
              (key, action, phase, at, at))
            store.conn.execute("INSERT INTO paper_sim_orders VALUES(?,?)", (order.lastrowid,phase))

    def snapshot(self, slot, within=5):
        return compare_ledgers(self.base.db_path, self.early.db_path, self.start+300*slot+within)

    def test_compares_confirmed_fills_in_complete_shared_rounds_including_no_trade(self):
        # Shared, early-only, base-only, neither, partial coverage, no observation, active.
        for slot in (-1,0,1,2,3,4,6):
            key = self.round(self.base, slot)
            self.round(self.early, slot, missing=240 if slot==4 else None)
            if slot in (-1,4,6):
                self.trade(self.base, key, 100, "SETTLED", 99)
            if slot == 0:
                self.trade(self.base, key, 90, "SETTLED", 1.5)
                self.order(self.base, key, "FILLED", 91)
                self.trade(self.early, key, 200, "SETTLED", 2)
                self.trade(self.early, key, 200, "SETTLED", -.01)
                self.order(self.early, key, "PARTIAL", 201)
                self.order(self.early, key, "FILLED", 160, action="FAK_SELL")
            if slot == 1:
                self.trade(self.early, key, 180, "SETTLED", -.3)
                self.trade(self.early, key, 180, "OPEN")  # partially sold remainder
                self.order(self.early, key, "FILLED", 180.5)
                self.order(self.base, key, "CANCELED", 90)
            if slot == 2:
                self.trade(self.base, key, 95, "SETTLED", .25)
                self.order(self.base, key, "FILLED", 96)
                self.order(self.early, key, "CONFIRMING", 60)
        report = self.snapshot(6)
        self.assertEqual((report["start_at"], report["rounds"],report["incomplete_rounds"],
                          report["active_rounds"]), (self.start,4,2,1))
        self.assertEqual(report["base"], dict(opened=2,open_rate=50,submitted=3,unfilled=1,
                                              skipped=1,unresolved=0,realized_pnl=1.75,
                                              excluded_opened=1,excluded_realized_pnl=99))
        self.assertEqual(report["early"], dict(opened=2,open_rate=50,submitted=3,unfilled=0,
                                               skipped=1,unresolved=2,realized_pnl=1.69,
                                               early_entries=1,early_submitted_and_filled=2,
                                               excluded_opened=0,excluded_realized_pnl=0))
        self.assertEqual(report["overlap"],dict(both=1,base_only=1,early_only=1,neither=1,
                         base_only_realized_pnl=.25,early_only_realized_pnl=-.3))
        self.assertEqual(report["pnl_delta"], -.06)

    def test_midround_launch_late_data_and_disjoint_or_empty_ledgers(self):
        with self.early.conn:
            self.early.conn.execute("UPDATE paper_sim_run SET started_at=?", (self.start+10,))
        for slot in (0, 1):
            self.round(self.base, slot)
            self.round(self.early, slot, late=60)
        self.round(self.base,2)
        self.round(self.early,3)
        report = self.snapshot(4)
        self.assertEqual((report["start_at"], report["rounds"], report["incomplete_rounds"]),
                         (self.start+300,0,3))
        self.assertIsNone(report["base"]["open_rate"])
        self.assertEqual(self.snapshot(0,20)["active_rounds"],0)
        self.assertFalse(compare_ledgers(self.base.db_path,self.base.db_path,self.start)["available"])
