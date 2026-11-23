import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from polybot2other.forecast_forward import DAY, ForecastForwardAudit, compare_forward


START = 1800000000


class ForwardTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.paths = [Path(temporary.name)/f"{arm}.db" for arm in ("candidate", "control")]
        self.arms = []
        for path in self.paths:
            c = sqlite3.connect(path)
            self.addCleanup(c.close)
            c.row_factory = sqlite3.Row
            c.executescript("""CREATE TABLE account(id,initial_balance,realized_pnl);
                INSERT INTO account VALUES(1,100,0);
                CREATE TABLE paper_sim_run(id,started_at);
                CREATE TABLE paper_sim_orders(order_id INTEGER PRIMARY KEY,phase TEXT,payload TEXT);
                CREATE TABLE paper_sim_consumed(token,action,fingerprint,levels);
                CREATE TABLE paper_orders(id INTEGER PRIMARY KEY,round_id,order_type,created_at,updated_at);
                CREATE TABLE trades(id INTEGER PRIMARY KEY,round_id,status,side,opened_at,settled_at,pnl,shares,stake);
                CREATE TABLE market_rounds(round_id PRIMARY KEY,started_at,ends_at);
                CREATE TABLE forecast_checkpoints(round_id,seconds_left);
                CREATE TABLE forecast_entry_trace(round_id,stage,details);
                CREATE TABLE forecast_realized_peak(id,pnl,stopped_at);
                INSERT INTO forecast_realized_peak VALUES(1,0,NULL);""")
            c.execute("INSERT INTO paper_sim_run VALUES(1,?)",(START-10,))
            import threading
            engine=SimpleNamespace(store=SimpleNamespace(conn=c,_lock=threading.RLock()),
                                   bot=SimpleNamespace(forecast_trial_boundary=START),
                                   policy=SimpleNamespace(depth_fraction=.5), info_cache={})
            with patch.dict("os.environ", {"POLYBOT2OTHER_FORECAST_LIMIT_START_AT":""}):
                audit=ForecastForwardAudit(engine)
            self.arms.append((c, audit))

    def test_shared_start_and_restart_and_before_start(self):
        c,a = self.arms[0]
        self.assertEqual(a.start_at, START)
        with patch.dict("os.environ", {"POLYBOT2OTHER_FORECAST_LIMIT_START_AT":"999"}):
            self.assertEqual(ForecastForwardAudit(a.engine).start_at, START)
        result=compare_forward(*self.paths,START-1)
        self.assertTrue(result["available"])
        self.assertEqual((result["status"],result["candidate"]["observed_rounds"]),("waiting",0))

    def test_partial_bid_missing_and_unconfirmed_buy_or_sell_count_once(self):
        c,a=self.arms[0]
        market=SimpleNamespace(round_id="r",ends_at=START+100)
        frame=SimpleNamespace(market=market,error=None,quote_error=lambda *args:None,
                              quotes={"Up":{"best_bid":.50,"tick_size":".01","min_order_size":1,
                                            "token_id":"up","bids":[{"price":.50,"size":8}]}})
        a.engine.info_cache["r"]={"tick_size":".01","fee_rate":0.,"fee_exponent":1}
        buy=dict(order_id=1,round_id="r",side="Up",action="BUY",fill={"cash":4.,"shares":8})
        c.execute("INSERT INTO paper_sim_orders VALUES(1,'CONFIRMING',?)",(json.dumps(buy),))
        a.observe(frame,START+1)
        self.assertEqual((a.previous["equity"],a.previous["unknown_shares"],a.drawdown),(98.,4.,2.))
        c.execute("DELETE FROM paper_sim_orders")
        c.execute("INSERT INTO trades VALUES(1,'r','OPEN','Up',?,NULL,0,8,4)",(START+1,))
        sell=dict(order_id=2,position_id=1,action="SELL",fill={"cash":2.8,"shares":7})
        c.execute("INSERT INTO paper_sim_orders VALUES(2,'CONFIRMING',?)",(json.dumps(sell),))
        a.observe(frame,START+2)
        self.assertEqual(a.previous["equity"],99.3)
        frame.error="offline"
        a.observe(frame,START+23)
        self.assertEqual(a.previous["equity"],98.8)
        self.assertEqual(a.previous["gap"],1)
        c.execute("DELETE FROM paper_sim_orders")
        a.observe(frame,START+24)
        self.assertEqual((a.previous["equity"],a.drawdown),(96.,4.))
        self.assertEqual(ForecastForwardAudit(a.engine).drawdown,4.)

    def test_real_ws_depth_reuses_token_specific_buy_metadata(self):
        c,a=self.arms[0]
        rid=self.put(c,START,unknown=True)
        frame=SimpleNamespace(error=None,quote_error=lambda *x: None,
            market=SimpleNamespace(round_id=rid,ends_at=START+300),
            quotes={"Up":{"token_id":"up","bids":[{"price":.5,"size":20}],"best_bid":.5}})
        a.engine.info_cache[rid]={"tick_size":"0.01","fee_rate":0.,"fee_exponent":1}
        with c:
            c.execute("UPDATE paper_sim_orders SET payload=?",(json.dumps(dict(
                minimum_shares=5,token="up",info={"tick_size":"0.01"})),))
        a.observe(frame,START+124)
        self.assertEqual((a.previous["liquidatable"],a.previous["unknown_shares"]),(2.5,0))
        frame.quotes["Up"]["token_id"]="other"
        a.observe(frame,START+125)
        self.assertEqual((a.previous["liquidatable"],a.previous["unknown_shares"]),(0.,5.))

    def put(self, conn, start, pnl=1., settle_after=6, gap=False, unknown=False):
        rid=f"btc-updown-5m-{start}"
        opened=start+121
        ended=opened+settle_after
        state="OPEN" if unknown else "SETTLED"
        order_id=conn.execute("SELECT count(*)+1 FROM paper_orders").fetchone()[0]
        conn.execute("INSERT INTO market_rounds VALUES(?,?,?)",(rid,start,start+300))
        conn.execute("INSERT INTO forecast_checkpoints VALUES(?,180)",(rid,))
        if not gap: conn.execute("INSERT INTO forecast_entry_trace VALUES(?,'window_seen',?)",(rid,'{"late_start":false}'))
        conn.execute("INSERT INTO paper_orders VALUES(?,?,'FAK',?,?)",(order_id,rid,opened-1,opened+1))
        conn.execute("INSERT INTO paper_sim_orders VALUES(?,'FILLED','{}')",(order_id,))
        conn.execute("INSERT INTO trades(round_id,status,side,opened_at,settled_at,pnl,shares,stake) VALUES(?,?,?,?,?,?,5,2.5)",
                     (rid,state,"Up",opened,None if unknown else ended,pnl))
        conn.commit()
        return rid

    def mark(self, conn, at):
        with conn:
            conn.execute("INSERT INTO forecast_limit_marks(at,equity,peak,drawdown,liquidatable,has_exposure,unknown_shares,gap) VALUES(?,100,100,1,0,0,0,0)",(at,))

    def test_split_fills_frozen_flat_cutoff_and_persisted_risk_stop(self):
        a,b=(x[0] for x in self.arms)
        for i in range(100):
            rid=self.put(a,START+i*300)
        with a:
            a.execute("UPDATE trades SET pnl=.6,shares=3 WHERE round_id=?",(rid,))
            a.execute("INSERT INTO trades(round_id,status,side,opened_at,settled_at,pnl,shares,stake) SELECT round_id,status,side,opened_at,settled_at,.4,2,1 FROM trades WHERE round_id=?",(rid,))
        maturity=START+7*DAY
        self.put(b,maturity-300,2.,230)
        # At day seven the control is still exposed: the cutoff cannot be sealed yet.
        self.assertIsNone(compare_forward(*self.paths,maturity)["cohort_cutoff"])
        end=maturity+300
        for c in (a,b):
            for at in range(maturity-220,end+1,10): self.mark(c,at)
        self.put(a,end,-1.,unknown=True)
        a.execute("UPDATE forecast_realized_peak SET stopped_at=? WHERE id=1",(end+20,))
        a.commit()
        result=compare_forward(*self.paths,end+25)
        self.assertEqual((result["status"],result["cohort_cutoff"]),("passed",end))
        self.assertFalse(result["candidate"]["flat_budget_exhausted"])
        self.assertEqual(result["candidate"]["closed_rounds"],100)
        self.assertEqual((result["candidate"]["first_50_pnl"],result["candidate"]["next_50_pnl"]),(50.,50.))
        self.assertAlmostEqual(result["candidate"]["stressed_pnl"],90.)
        with b: b.execute("DELETE FROM forecast_limit_marks WHERE at BETWEEN ? AND ?",(maturity-150,maturity+10))
        self.assertEqual(compare_forward(*self.paths,end+600)["status"],"incomplete")

    def test_last_settlement_after_last_observation_contributes_to_drawdown(self):
        c,_ = self.arms[0]
        self.put(c,START,-4)
        self.mark(c,START+125)
        with c: c.execute("UPDATE forecast_limit_marks SET peak=105")
        self.assertEqual(compare_forward(*self.paths,START+128)["candidate"]["marked_drawdown"],9.)

    def test_coverage_unknown_market_and_stopped_budget(self):
        c,_=self.arms[0]
        self.put(c,START,gap=True)
        with c: c.execute("UPDATE forecast_realized_peak SET stopped_at=? WHERE id=1",(START+399,))
        result=compare_forward(*self.paths,START+601)
        self.assertEqual(result["status"],"failed")
        self.assertEqual(result["candidate"]["data_gap_rounds"],2)
