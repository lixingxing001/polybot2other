"""Isolated strategy wallets consume causal, finite public liquidity and official results."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from polybot2other.forecast_paper import ForecastDecision
from polybot2other.models import MarketRound
from polybot2other.optimized_reference import SPOT, TWAP
from polybot2other.shared_market_inputs import MarketInputs
from polybot2other.strategy_league import LeagueEngine
from polybot2other.strategy_league_policies import POLICIES


START = 1800000000


class LeagueEngineTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name)/"league.sqlite3"
        self.now = START + 130
        self.market = self.market_at(START)
        self.info = {"fee_rate":.07,"fee_exponent":1,"tick_size":"0.01"}
        self.rules = {s:{"token_id":s.lower(),"min_order_size":5,"tick_size":"0.01"} for s in ("Up","Down")}
        self.policies = (POLICIES[0],)
        self.engine = self.engine_for(START)

    def tearDown(self):
        self.engine.close()
        self.dir.cleanup()

    def market_at(self, t):
        return MarketRound(f"btc-updown-5m-{t}", "BTC", t,t+300,100, condition_id="test",up_token="up",down_token="down")

    def engine_for(self, start):
        return LeagueEngine(self.path,start_at=start,policies=self.policies,clock=lambda:self.now)

    def feed(self,t, *, stamp=None, up_score=.86, ask=.56, bid=.55, ask_size=80, bid_size=80, invalid=False):
        self.now=t
        qms=1000*(t if stamp is None else stamp)
        books={}
        for side,b,a in [("Up",bid,ask),("Down",round(1-ask,2),round(1-bid,2))]:
            books[side]={"token_id":side.lower(),"received_ms":qms,"source_ms":qms,
                         "best_bid":b,"best_ask":a,"bids":[{"price":b,"size":bid_size}],
                         "asks":[{"price":a,"size":ask_size}]}
        tick={"source_ms":t*1000,"received_ms":t*1000,"price":100.1}
        frame=MarketInputs(self.market,{"ready":True,"protocol_verified":True,"latest":{SPOT:tick,TWAP:tick}}, {},books,
                          t,str(t),"gap" if invalid else None)
        decision=ForecastDecision("WAIT","test",self.market.round_id,t,str(t),up_score=up_score, projected_final_twap=100.1)
        self.engine.step(frame,decision,self.info,self.rules)

    def one(self,table):
        return self.engine.db.execute(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT 1").fetchone()

    def test_start_boundary_resumes_independent_wallets_with_identical_cash(self):
        self.engine.close()
        self.path = Path(self.dir.name)/"many.sqlite3"
        self.policies=POLICIES
        self.engine=self.engine_for(START+300)
        self.feed(START+130)
        self.assertIsNone(self.one("league_rounds"))
        self.market=self.market_at(START+300)
        self.feed(START+430)
        self.feed(START+431.2)
        self.assertEqual(self.engine.db.execute("select count(*) from league_accounts").fetchone()[0],25)
        positions=self.engine.db.execute("select count(*),count(distinct strategy_id),min(cost),max(cost) from league_positions").fetchone()
        self.assertGreater(positions[0],3)
        self.assertEqual(positions[0],positions[1])
        self.assertEqual(positions[2],positions[3])
        self.engine.close()
        self.engine=self.engine_for(START+600)
        self.assertEqual(self.engine.starts_at,START+300)
        self.feed(START+435)
        self.assertEqual(self.engine.db.execute("select count(*) from league_positions").fetchone()[0],positions[0])
        self.assertEqual(self.engine.db.execute("select count(*) from league_orders where action='BUY'").fetchone()[0],positions[0])

    def test_fresh_new_quote_and_minimum_depth_rules(self):
        del self.rules["Up"]["min_order_size"]
        self.feed(START+130)
        self.assertIsNone(self.one("league_orders"))
        self.rules["Up"]["min_order_size"]=500
        self.feed(START+131)
        self.assertIsNone(self.one("league_orders"))
        self.rules["Up"]["min_order_size"]=5
        self.feed(START+132)
        self.assertEqual(self.one("league_orders")["status"],"PENDING")
        self.feed(START+133.1,stamp=START+132)
        self.assertIsNone(self.one("league_positions"))
        self.feed(START+134,ask_size=4)
        fill=self.one("league_orders")
        self.assertEqual(fill["status"],"PARTIAL")
        self.assertEqual(fill["filled_shares"],2)
        self.assertGreater(fill["fee"],0)
        self.assertAlmostEqual(self.one("league_accounts")["cash"],100-fill["cash"])

    def test_arrival_after_delay_with_worse_quote_cancels_and_no_reentry_when_exposed(self):
        self.feed(START+130)
        self.feed(START+131.1,ask=.64,bid=.62)
        self.assertEqual(self.one("league_orders")["status"],"CANCELLED")
        self.feed(START+132)
        self.assertEqual(self.engine.db.execute("select count(*) from league_orders").fetchone()[0],1)
        self.feed(START+136)
        self.feed(START+142)
        self.assertEqual(self.one("league_orders")["status"],"CANCELLED")
        self.assertIsNone(self.one("league_positions"))

    def test_partial_sell_does_not_reconsume_same_depth_and_settles_remainder_once(self):
        self.feed(START+130)
        self.feed(START+131.1)
        bought=self.one("league_positions")
        # Only 1 share is accessible after the 50% shared-ladder haircut.
        self.feed(START+137,up_score=.3,bid_size=2)
        self.feed(START+138.2,up_score=.3,bid_size=2)
        sold=self.one("league_orders")
        self.assertEqual((sold["action"],sold["status"],sold["filled_shares"]),("SELL","PARTIAL",1))
        self.feed(START+144,up_score=.3,bid_size=2)
        self.assertEqual(self.one("league_orders")["id"],sold["id"])
        self.assertEqual(self.engine.pending_rounds(START+301),[self.market.round_id])
        self.assertFalse(self.engine.settle(self.market.round_id,"Up",START+299,source="polymarket_official"))
        self.assertFalse(self.engine.settle(self.market.round_id,"Up",START+302,source="spot_estimate"))
        self.assertTrue(self.engine.settle(self.market.round_id,"Up",START+302,source="polymarket_official"))
        self.assertFalse(self.engine.settle(self.market.round_id,"Up",START+306,source="polymarket_official"))
        pos=self.one("league_positions")
        self.assertEqual((pos["shares"],pos["stake"],pos["closed_at"]),(0,0,START+302))
        self.assertAlmostEqual(pos["pnl"],bought["shares"]-1+sold["cash"]-bought["cost"],places=5)
        acct=self.one("league_accounts")
        self.assertAlmostEqual(acct["equity"],100+pos["pnl"],places=5)
        self.assertAlmostEqual(pos["fees"],bought["fees"]+sold["fee"],places=5)
        self.assertEqual(self.engine.pending_rounds(START+306),[])

    def test_restart_releases_pending_and_immutable_keyset(self):
        self.feed(START+130)
        self.engine.cancel_pending(START+130.5)
        self.feed(START+131.5)
        self.assertEqual(self.one("league_orders")["status"],"CANCELLED")
        self.assertEqual(self.one("league_accounts")["cash"],100)
        with self.assertRaises(ValueError):
            LeagueEngine(self.path,start_at=START,policies=[replace(self.policies[0],cap=.55)])

    def test_stopped_legacy_wallet_resumes_once_and_preserves_history(self):
        self.assertEqual(self.one("league_admission_policy")["unrestricted_at"], START)
        self.feed(START+130)
        self.feed(START+132)
        self.engine.settle(self.market.round_id,"Down",START+301,source="polymarket_official")
        with self.engine.db:
            self.engine.db.execute("DROP TABLE league_admission_policy")
            self.engine.db.execute("DROP TABLE league_risk_releases")
            self.engine.db.execute("""CREATE UNIQUE INDEX league_one_position
                ON league_positions(strategy_id) WHERE closed_at IS NULL""")
            self.engine.db.execute("""CREATE UNIQUE INDEX league_one_order
                ON league_orders(strategy_id) WHERE status='PENDING'""")
            self.engine.db.execute("UPDATE league_accounts SET stopped_at=?,last_reason='回撤额度不足，已停止新买入'",
                                   (START+302,))
        account = dict(self.one("league_accounts"))
        historical = {table: tuple(self.one(table)) for table in
                      ("league_positions", "league_orders", "league_pnl_events", "league_run")}
        self.engine.close()
        self.now = START+310
        self.engine = self.engine_for(START+600)
        self.assertEqual(self.one("league_admission_policy")["unrestricted_at"], self.now)
        release = tuple(self.one("league_risk_releases"))
        self.assertEqual(release, (self.policies[0].key, START+302, self.now))
        self.assertIsNone(self.one("league_accounts")["stopped_at"])
        for field in ("cash","realized","realized_peak","equity","equity_peak","max_drawdown","last_order_at"):
            self.assertEqual(self.one("league_accounts")[field], account[field])
        for table, record in historical.items():
            self.assertEqual(tuple(self.one(table)), record)
        self.market = self.market_at(START+300)
        self.feed(START+430)
        self.feed(START+432)
        self.assertEqual(self.one("league_orders")["status"], "FILLED")
        self.engine.close()
        self.engine = self.engine_for(START+900)
        self.assertEqual(tuple(self.one("league_risk_releases")), release)
        self.assertEqual(self.one("league_admission_policy")["unrestricted_at"], START+310)

    def test_fixed_budget_after_daily_loss_drawdown_and_negative_cash(self):
        costs = []
        for i in range(24):
            t = START+i*300
            self.market = self.market_at(t)
            self.feed(t+130)
            self.feed(t+132)
            buy = self.one("league_orders")
            self.assertEqual((buy["round_id"],buy["status"]), (self.market.round_id,"FILLED"))
            costs.append(buy["cash"])
            self.assertTrue(self.engine.settle(self.market.round_id,"Down",t+301,source="polymarket_official"))
        self.assertEqual(len(set(costs)),1)
        account = self.one("league_accounts")
        self.assertIsNone(account["stopped_at"])
        self.assertLess(account["cash"], -costs[-1])
        self.assertAlmostEqual(account["cash"], 100-sum(costs))
        self.assertAlmostEqual(account["realized"], -sum(costs))
        self.assertAlmostEqual(account["equity"], account["cash"])
        self.assertAlmostEqual(account["max_drawdown"], sum(costs))

    def test_late_official_result_does_not_block_or_discard_next_market_value(self):
        self.feed(START+130)
        self.feed(START+132)
        old = self.one("league_positions")
        self.market = self.market_at(START+300)
        self.feed(START+430)
        self.feed(START+432,bid_size=8)
        active = self.one("league_positions")
        self.assertEqual(active["round_id"],self.market.round_id)
        self.assertEqual(self.engine.db.execute("SELECT count(*) FROM league_positions WHERE closed_at IS NULL").fetchone()[0],2)
        before = self.one("league_accounts")
        self.assertGreater(before["equity"],before["cash"])
        self.assertAlmostEqual(before["unknown_shares"], old["shares"]+active["shares"]-4)
        self.engine.close()
        self.engine = self.engine_for(START+600)
        self.assertTrue(self.engine.settle(old["round_id"],"Up",START+434,source="polymarket_official"))
        after = self.one("league_accounts")
        self.assertAlmostEqual(after["equity"],before["equity"]+old["shares"],places=5)
        self.assertAlmostEqual(after["unknown_shares"],active["shares"]-4)
        self.feed(START+440,up_score=.3)
        self.feed(START+442,up_score=.3)
        self.assertEqual(self.one("league_orders")["action"], "SELL")
        self.assertTrue(self.engine.settle(active["round_id"],"Down",START+610,source="polymarket_official"))
        account = self.one("league_accounts")
        self.assertAlmostEqual(account["equity"],100+account["realized"],places=5)
        self.assertEqual(account["unknown_shares"], 0.)

    def test_quote_loss_marks_unsellable_open_shares_zero_and_flags_gap(self):
        self.feed(START+130)
        self.feed(START+131.1)
        self.feed(START+148,invalid=True)
        acct=self.one("league_accounts")
        self.assertGreater(acct["unknown_shares"],0)
        self.assertEqual(acct["cash"],acct["equity"])
        self.assertGreater(acct["max_drawdown"],4)
        self.assertTrue(self.one("league_rounds")["gap"])

    def test_gap_only_covers_tradable_period_and_required_history(self):
        self.feed(START+5,invalid=True)
        self.feed(START+85,invalid=True)
        self.feed(START+92)
        self.assertFalse(self.one("league_rounds")["gap"])
        self.feed(START+109,invalid=True)
        self.assertTrue(self.one("league_rounds")["gap"])

    def test_lost_tail_is_recognized_after_rollover(self):
        for delta in range(1,280,10):
            self.feed(START+delta)
        self.assertFalse(self.one("league_rounds")["gap"])
        self.market=self.market_at(START+300)
        self.feed(START+301)
        self.assertTrue(self.engine.db.execute("select gap from league_rounds where starts_at=?",(START,)).fetchone()[0])
