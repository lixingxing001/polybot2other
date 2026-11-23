import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from polybot2other.strategy_league import LeagueEngine
from polybot2other.strategy_league_policies import POLICIES, CONTROL_KEY
from polybot2other.strategy_league_report import DAY, StrategyLeagueReport, read_league


class LeagueReportTests(unittest.TestCase):
    start = 1_795_050_600

    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path=Path(tmp.name)/"strategy-league.sqlite3"
        self.engine=LeagueEngine(self.path,start_at=self.start)
        self.addCleanup(self.engine.close)
        self.c=self.engine.db
        self.keys=[p.key for p in POLICIES if p.family==POLICIES[1].family]
        self.bot=SimpleNamespace(store=SimpleNamespace(db_path=str(Path(tmp.name)/"main.sqlite3")),settings=SimpleNamespace(strategy_league_enabled=True))
        self.count=0

    def markets(self, days):
        rounds=((str(i),self.start+i*300,self.start+(i+1)*300,self.start+i*300,self.start+i*300+299,
                 self.start+i*300+299,0) for i in range(int(days*DAY/300)))
        with self.c:
            self.c.executemany("INSERT OR IGNORE INTO league_rounds(round_id,starts_at,ends_at,first_seen,last_seen,last_valid,gap) VALUES(?,?,?,?,?,?,?)",rounds)

    def trade(self,key,at,pnl,close=None):
        rid=str(int((at-self.start)//300));self.count+=1
        with self.c:
            self.c.execute("""INSERT INTO league_positions(strategy_id,round_id,side,opened_at,closed_at,shares,
                stake,bought_shares,cost,proceeds,pnl,fees) VALUES(?,?,'Up',?,?,0,0,5,3,?,?,.02)""",
                (key,rid,at,at+3 if close is None else close,3+pnl,pnl))

    def batch(self,key,since,n,profit,span=6*DAY):
        for i in range(n):
            self.trade(key,since+int(i*span/n)+25,profit)

    def report(self,at):
        result=read_league(self.path,at)
        return {r["key"]:r for r in result["arms"]}

    def test_read_only_unavailable_and_empty_wallets(self):
        empty=Path(tempfile.gettempdir())/"missing-strategy-league-unit-file.db"
        with self.assertRaises(sqlite3.OperationalError):
            read_league(empty,self.start)
        self.assertFalse(empty.exists())
        status=read_league(self.path,self.start-60)
        self.assertEqual((status["candidate_count"],status["family_count"],len(status["arms"])),(24,6,25))
        self.assertTrue(all(a["cash"]==100 and a["position"] is None for a in status["arms"]))
        self.bot.settings.strategy_league_enabled=False
        self.assertFalse(StrategyLeagueReport(self.bot).snapshot()["enabled"])
        with self.c:
            self.c.execute("UPDATE league_run SET catalog_hash='other'")
        self.assertFalse(read_league(self.path,self.start)["available"])

    def test_frozen_family_selection_and_untouched_validation(self):
        end=self.start+7*DAY
        self.markets(15)
        a,b=self.keys[:2]
        self.batch(a,self.start,100,.8)
        self.batch(b,self.start,100,.2)
        self.batch(CONTROL_KEY,self.start,100,.1)
        first=self.report(end+.5)
        self.assertEqual(first[a]["grade"]["code"],"validation")
        self.assertEqual(first[b]["grade"]["code"],"reserve")
        self.batch(a,end,100,.4)
        self.batch(b,end,100,5.)
        self.batch(CONTROL_KEY,end,100,.12)
        mature=self.report(end+7*DAY)
        self.assertEqual(mature[a]["grade"]["code"],"focus")
        self.assertEqual(mature[b]["grade"]["code"],"reserve")
        self.assertEqual(mature[a]["grade"]["validation"]["rounds"],100)
        self.assertAlmostEqual(mature[a]["grade"]["validation"]["stressed"],30)
        self.assertEqual(mature[a]["shared_closed"],200)
        with self.c:
            self.c.execute("UPDATE league_accounts SET max_drawdown=12 WHERE strategy_id=?",(a,))
        after=self.report(end+7*DAY)[a]
        self.assertEqual(after["grade"]["code"],"focus")
        self.assertEqual(after["max_drawdown"],12)

    def test_censored_samples(self):
        end=self.start+7*DAY
        self.markets(8)
        a=self.keys[0]
        self.batch(a,self.start,100,.5)
        self.trade(CONTROL_KEY,end-200,-2,close=end+80)
        self.assertEqual(self.report(end)[a]["grade"]["code"],"collecting")
        self.assertEqual(self.report(end+301)[a]["grade"]["screen"]["end"],end+300)

    def test_release_preserves_totals_and_compares_new_policy_on_complete_markets(self):
        self.markets(16)
        end=self.start+7*DAY
        release=end+400
        boundary=end+600
        a=self.keys[0]
        self.batch(a,self.start,100,.5)
        self.batch(CONTROL_KEY,self.start,100,.2)
        previous_stop=end-500
        with self.c:
            self.c.execute("UPDATE league_admission_policy SET unrestricted_at=?",(release,))
            self.c.execute("INSERT INTO league_risk_releases(strategy_id,stopped_at,released_at) VALUES(?,?,?)",
                           (a,previous_stop,release))
        self.trade(a,release+2,1.)
        self.assertEqual(self.report(end)[a]["grade"]["code"],"stopped")
        just_released=read_league(self.path,release+10)
        self.assertTrue(just_released["unrestricted"])
        self.assertEqual(just_released["screen_start"],boundary)
        candidate=next(arm for arm in just_released["arms"] if arm["key"]==a)
        self.assertEqual(candidate["past_stop"]["stopped_at"], previous_stop)
        self.assertEqual(candidate["past_stop"]["released_at"], release)
        self.assertIsNone(candidate["stopped_at"])
        self.assertEqual(candidate["grade"]["code"],"collecting")
        self.assertEqual(candidate["closed"],101)
        self.assertEqual(candidate["closed_pnl"],51.)
        self.assertEqual(candidate["screen_to_date"],{"rounds":0,"pnl":0.,"excess":0.})
        self.batch(a,boundary,100,.8)
        self.trade(a,boundary+6*DAY,-12)
        next_week=self.report(boundary+7*DAY)[a]
        self.assertEqual(next_week["grade"]["code"],"validation")
        self.assertEqual(next_week["grade"]["screen"]["drawdown"],12.)
        self.assertEqual(next_week["screen_to_date"],{"rounds":101,"pnl":68.,"excess":68.})

    def test_all_unsettled_round_positions_are_reported(self):
        a=self.keys[0]
        for i in range(2):
            self.trade(a,self.start+300*i+10,.1)
        with self.c:
            self.c.execute("UPDATE league_positions SET closed_at=NULL,shares=3 WHERE strategy_id=?",(a,))
        positions=self.report(self.start+610)[a]["positions"]
        self.assertEqual({(p["round_id"],p["shares"]) for p in positions},{("0",3),("1",3)})

    def test_missing_coverage_blocks_selection(self):
        a=self.keys[0]
        self.markets(3)
        self.batch(a,self.start,100,.5)
        arm=self.report(self.start+7*DAY)[a]
        self.assertEqual(arm["grade"]["code"],"coverage")
        self.assertLess(arm["grade"]["screen"]["coverage"]["ratio"],.5)

    def test_pending_empty_buy_blocks_decision_until_next_flat_boundary(self):
        a=self.keys[0]
        end=self.start+7*DAY
        self.markets(8)
        self.batch(a,self.start,100,.5)
        with self.c:
            self.c.execute("""INSERT INTO league_orders(strategy_id,round_id,side,action,limit_price,submitted_at,
                status,reason,token,source_ms,received_ms,notional_budget,requested_shares,tick_size,min_size,
                fee_rate,fee_exponent) VALUES(?,?,'Up','BUY',.50,?,'PENDING','test','tok',0,0,5,10,'.01',5,.01,2)""",
                (a,"unmatched",end-1))
        self.assertEqual(self.report(end+1)[a]["grade"]["code"],"collecting")
        with self.c:
            self.c.execute("UPDATE league_orders SET status='CANCELLED',finished_at=?",(end+2,))
        self.assertEqual(self.report(end+301)[a]["grade"]["screen"]["end"],end+300)
