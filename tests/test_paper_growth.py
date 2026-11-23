"""Growth descriptions use isolated Paper fixtures and never invoke execution."""
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
import urllib.error
import urllib.request

from polybot2other.paper_growth import PaperGrowthMonitor, TIMEZONE, build_growth_snapshot, build_strategy_snapshot, windows_at
from polybot2other.web import DashboardServer, Handler


NOW = datetime(2026, 11, 18, 12, tzinfo=TIMEZONE).timestamp()


def trade(offset, pnl=2., *, market=None, strategy="MAIN", stake=5.):
    return {"round_id": market or f"round-{offset}", "strategy_id": strategy,
            "pnl": pnl, "stake": stake, "settled_at": NOW + offset}


def snapshot(rows, now=NOW, created_at=NOW-172800, positions=None, strategy="MAIN"):
    account = {"strategy_id": strategy, "name": strategy, "created_at": created_at}
    return build_strategy_snapshot(account, rows, now=now, open_positions=positions)


def multi_snapshot(rows, accounts=None, positions=None):
    accounts = accounts or [{"strategy_id": k, "name": k, "created_at": NOW-172800}
                            for k in sorted({r["strategy_id"] for r in rows})]
    return build_growth_snapshot(accounts, rows, now=NOW, open_positions=positions)


class GrowthCalculationsTests(unittest.TestCase):
    def test_windows_use_shanghai_midnight_and_completed_night(self):
        morning = datetime(2026, 11, 18, 2, tzinfo=TIMEZONE).timestamp()
        windows = windows_at(morning)
        self.assertEqual(datetime.fromtimestamp(windows['today'][0], TIMEZONE).hour, 0)
        self.assertEqual(datetime.fromtimestamp(windows['night'][0], TIMEZONE).day, 17)
        self.assertEqual(windows_at(NOW)['night'][0], windows_at(NOW)['today'][0])

    def test_half_open_windows_exclude_future_and_do_not_double_count_boundary(self):
        r = snapshot([trade(-3600, 4), trade(-3599, 2), trade(-3601, -1), trade(0, 999), trade(1, 999)])
        self.assertEqual(r['windows']['last1h']['pnl'], 6)
        self.assertEqual(r['windows']['previous1h']['pnl'], -1)
        self.assertEqual(r['windows']['today']['pnl'], 5)

    def test_daily_results_do_not_include_previous_day(self):
        now = datetime(2026, 11, 18, 0, 30, tzinfo=TIMEZONE).timestamp()
        rows = [dict(trade(-1, 3), settled_at=now-600), dict(trade(-2, 100), settled_at=now-2400)]
        r = snapshot(rows, now=now)
        self.assertEqual(r['windows']['today']['pnl'], 3)
        self.assertEqual(r['windows']['last1h']['pnl'], 103)

    def test_each_strategy_counts_only_its_own_trade_in_a_shared_market(self):
        result = multi_snapshot([trade(-60, -5, market='one', strategy=str(i)) for i in range(5)])
        self.assertEqual(result['scope'], 'paper_by_strategy')
        self.assertNotIn('state', result)
        self.assertNotIn('windows', result)
        self.assertEqual(result['strategy_count'], 5)
        for r in result['strategies']:
            self.assertEqual(r['windows']['last1h']['trades'], 1)
            self.assertEqual(r['windows']['last1h']['markets'], 1)
            self.assertEqual(r['windows']['last1h']['pnl'], -5)
            self.assertEqual(r['state']['code'], 'INSUFFICIENT')
            self.assertEqual(r['largest_recent_market_loss']['pnl'], -5)

    def test_one_profitable_strategy_and_one_losing_strategy_remain_opposite(self):
        offsets = [-200-i*200 for i in range(3)] + [-4000-i*200 for i in range(3)]
        rows = [trade(offset, 2, strategy='WINNER') for offset in offsets]
        rows += [trade(offset, -20, strategy='LOSER') for offset in offsets]
        strategies = {r['strategy_id']:r for r in multi_snapshot(rows)['strategies']}
        self.assertEqual(strategies['WINNER']['state']['label'], '最近持续盈利')
        self.assertEqual(strategies['LOSER']['state']['label'], '最近持续亏损')
        self.assertEqual(strategies['WINNER']['windows']['today']['pnl'], 12)
        self.assertEqual(strategies['LOSER']['windows']['today']['pnl'], -120)

    def test_other_strategy_history_and_recent_settlement_do_not_fill_missing_samples(self):
        rows = [trade(-200-i*200, 3) for i in range(3)] + [trade(-4000-i*200, 3) for i in range(3)]
        rows += [trade(-30, 9, strategy='NEW')]
        accounts = [{'strategy_id':'MAIN','name':'main','created_at':NOW-172800},
                    {'strategy_id':'NEW','name':'new','created_at':NOW-300},
                    {'strategy_id':'IDLE','name':'idle','created_at':NOW-172800}]
        a = {r['strategy_id']:r for r in multi_snapshot(rows, accounts)['strategies']}
        self.assertEqual(a['MAIN']['state']['code'], 'STRONG')
        self.assertEqual(a['NEW']['state']['code'], 'INSUFFICIENT')
        self.assertEqual(a['NEW']['windows']['last3h']['markets'], 1)
        self.assertEqual(a['IDLE']['state']['code'], 'INACTIVE')
        self.assertIsNone(a['IDLE']['latest_settlement_at'])

    def test_single_correlated_loss_after_good_run_stays_pullback(self):
        rows = [trade(-4000-i*400, 10) for i in range(6)]
        rows.append(trade(-60, -5, market='same-loss'))
        rows += [trade(-60-i, -5, market='same-loss', strategy=str(i)) for i in range(5)]
        r = snapshot(rows)
        self.assertEqual(r['state']['code'], 'PULLBACK')
        self.assertEqual(r['windows']['last3h']['losing_markets'], 1)

    def test_persistent_decline_requires_distinct_markets_and_two_losing_hours(self):
        rows = [trade(-200-i*200, -5) for i in range(3)]
        rows += [trade(-4000-i*200, -5) for i in range(3)]
        self.assertEqual(snapshot(rows)['state']['code'], 'WEAKENING')

    def test_declining_three_hours_without_prior_hour_loss_is_not_persistent(self):
        rows = [trade(-200-i*200, -10) for i in range(3)] + [trade(-4000-i*200, 2) for i in range(3)]
        self.assertEqual(snapshot(rows)['state']['code'], 'PULLBACK')

    def test_strong_requires_multiple_positive_windows(self):
        rows = [trade(-200-i*200, 2) for i in range(3)] + [trade(-4000-i*200, 2) for i in range(3)]
        self.assertEqual(snapshot(rows)['state']['code'], 'STRONG')

    def test_turning_positive_is_distinct_from_earning_faster(self):
        recent = [trade(-200-i*200, 4) for i in range(3)]
        previous = [trade(-4000-i*200, -2) for i in range(3)]
        self.assertEqual(snapshot(recent+previous)['state']['label'], '最近开始盈利')
        previous = [trade(-4000-i*200, 2) for i in range(3)]
        self.assertEqual(snapshot(recent+previous)['state']['label'], '最近赚得更快了')

    def test_double_stake_alone_does_not_fake_acceleration(self):
        recent = [trade(-200-i*200, 4, stake=10) for i in range(3)]
        previous = [trade(-4000-i*200, 2, stake=5) for i in range(3)]
        self.assertEqual(snapshot(recent+previous)['state']['code'], 'STRONG')

    def test_same_round_partial_settlements_do_not_invent_day_peak(self):
        r = snapshot([trade(-200, 8, market='one'), trade(-100, -10, market='one')])
        self.assertEqual(r['today_curve']['peak_pnl'], 0)
        self.assertEqual(r['today_curve']['giveback'], 2)
        self.assertIsNone(r['today_curve']['giveback_pct_of_peak_profit'])

    def test_peak_and_loss_are_not_offset_by_another_strategy(self):
        rows = [trade(-200, 10, market='one'), trade(-100, -4, market='two')]
        rows += [trade(-200, -100, market='one', strategy='OTHER'), trade(-100, 400, market='two', strategy='OTHER')]
        r = snapshot(rows)
        self.assertEqual(r['today_curve']['peak_pnl'], 10)
        self.assertEqual(r['today_curve']['giveback'], 4)
        self.assertEqual(r['largest_recent_market_loss']['pnl'], -4)

    def test_peak_giveback_is_not_account_balance_or_lifetime_profit(self):
        r = snapshot([trade(-400, 10), trade(-300, -3), trade(-200, 2), trade(-100, -4)])
        self.assertEqual(r['windows']['today']['pnl'], 5)
        self.assertEqual(r['today_curve']['peak_pnl'], 10)
        self.assertEqual(r['today_curve']['giveback'], 5)
        self.assertEqual(r['today_curve']['giveback_pct_of_peak_profit'], 50)

    def test_empty_and_inactive_data_are_not_claimed_as_weak(self):
        self.assertEqual(snapshot([])['state']['code'], 'INACTIVE')
        self.assertIsNone(snapshot([])['windows']['last1h']['win_rate'])
        self.assertEqual(snapshot([trade(-8000, 10)])['state']['code'], 'INACTIVE')

    def test_new_account_has_explicit_partial_coverage(self):
        rows = [trade(-60-i*30, 5) for i in range(8)]
        r = snapshot(rows, created_at=NOW-1000)
        self.assertFalse(r['windows']['last3h']['complete_history'])
        self.assertEqual(r['state']['code'], 'INSUFFICIENT')

    def test_open_positions_are_disclosed_without_estimated_pnl(self):
        r = snapshot([trade(-200, 3)], positions=[{'round_id':'open','stake':9.5,'strategy_id':'MAIN'},
                                                {'round_id':'other','stake':100,'strategy_id':'OTHER'}])
        self.assertEqual(r['windows']['today']['pnl'], 3)
        self.assertEqual(r['open_positions'], 1)
        self.assertEqual(r['open_cost'], 9.5)

    def test_no_combined_state_even_with_empty_input(self):
        r = build_growth_snapshot([], [], now=NOW)
        self.assertFalse(r['available'])
        self.assertEqual(r['strategies'], [])
        self.assertNotIn('state', r)
        self.assertFalse(r['policy']['controls_orders'])


class GrowthReadOnlyServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'paper.sqlite3'
        self.conn = sqlite3.connect(self.path)
        self.addCleanup(self.conn.close)
        self.conn.executescript('''
            CREATE TABLE account(id INTEGER PRIMARY KEY,created_at REAL);
            CREATE TABLE trades(id INTEGER PRIMARY KEY,round_id TEXT,stake REAL,pnl REAL,status TEXT,opened_at REAL,settled_at REAL);
        ''')
        self.conn.execute('INSERT INTO account VALUES(1,?)', (NOW-172800,))
        self.conn.execute('INSERT INTO trades VALUES(1,?,?,?,?,?,?)', ('one',5,2,'SETTLED',NOW-500,NOW-200))
        self.conn.commit()
        self.bot = SimpleNamespace(store=SimpleNamespace(db_path=self.path), strategy_experiments=None)
        self.clock = NOW
        self.monitor = PaperGrowthMonitor(self.bot, clock=lambda:self.clock)

    def test_reads_existing_paper_ledger_without_writing(self):
        before = self.path.read_bytes()
        result = self.monitor.snapshot()
        self.assertTrue(result['available'])
        self.assertEqual(result['strategies'][0]['windows']['today']['pnl'], 2)
        self.assertEqual(self.path.read_bytes(), before)

    def test_cache_expires_and_clock_rollback_does_not_keep_old_state(self):
        first = self.monitor.snapshot()
        self.conn.execute('UPDATE trades SET pnl=4');self.conn.commit()
        self.assertIs(self.monitor.snapshot(), first)
        self.clock += 11
        self.assertEqual(self.monitor.snapshot()['strategies'][0]['windows']['today']['pnl'], 4)
        self.clock -= 12
        self.assertLess(self.monitor.snapshot()['as_of'], first['as_of'])

    def test_one_missing_ledger_does_not_hide_or_change_another_strategy(self):
        missing = Path(self.tmp.name)/'missing.sqlite3'
        runner = SimpleNamespace(_lock=threading.RLock(), variants=[SimpleNamespace(variant_id='OTHER',combo='other')],
                                 _bots={'OTHER':SimpleNamespace(store=SimpleNamespace(db_path=missing))})
        self.bot.strategy_experiments=runner
        r=self.monitor.snapshot()
        self.assertTrue(r['available']);self.assertFalse(r['complete']);self.assertFalse(missing.exists())
        a={x['strategy_id']:x for x in r['strategies']}
        self.assertTrue(a['MAIN']['available']);self.assertEqual(a['MAIN']['windows']['today']['pnl'],2)
        self.assertFalse(a['OTHER']['available']);self.assertEqual(a['OTHER']['state']['code'],'UNAVAILABLE')
        self.assertNotIn('windows',a['OTHER'])

    def test_not_yet_initialized_strategy_is_visible_as_unavailable(self):
        self.bot.strategy_experiments=SimpleNamespace(_lock=threading.RLock(),
            variants=[SimpleNamespace(variant_id='NEW',combo='new')],_bots={})
        r=self.monitor.snapshot()
        self.assertEqual(r['strategy_count'],2)
        self.assertEqual(r['strategies'][1]['strategy_id'],'NEW')
        self.assertEqual(r['strategies'][1]['state']['code'],'UNAVAILABLE')

    def test_separate_databases_keep_their_latest_settlement_and_counts(self):
        other=Path(self.tmp.name)/'other.sqlite3'
        dst=sqlite3.connect(other);self.conn.backup(dst)
        dst.execute('UPDATE trades SET settled_at=?,pnl=?',(NOW-9000,-30));dst.commit();dst.close()
        self.bot.strategy_experiments=SimpleNamespace(_lock=threading.RLock(),
            variants=[SimpleNamespace(variant_id='OTHER',combo='other')],
            _bots={'OTHER':SimpleNamespace(store=SimpleNamespace(db_path=other))})
        a={x['strategy_id']:x for x in self.monitor.snapshot()['strategies']}
        self.assertEqual(a['OTHER']['state']['code'],'INACTIVE')
        self.assertEqual(a['OTHER']['windows']['last1h']['trades'],0)
        self.assertEqual(a['MAIN']['windows']['last1h']['pnl'],2)
        self.assertNotEqual(a['MAIN']['latest_settlement_at'],a['OTHER']['latest_settlement_at'])

    def test_same_database_is_not_double_counted(self):
        self.bot.strategy_experiments=SimpleNamespace(_lock=threading.RLock(),
            variants=[SimpleNamespace(variant_id='ALIAS',combo='alias')],
            _bots={'ALIAS':SimpleNamespace(store=SimpleNamespace(db_path=self.path))})
        r=self.monitor.snapshot()
        self.assertEqual(r['strategy_count'],1)
        self.assertEqual(r['strategies'][0]['windows']['today']['pnl'],2)

    def test_get_head_and_rejected_post_do_not_mutate_ledger(self):
        server=DashboardServer(('127.0.0.1',0),Handler,self.bot)
        server.paper_growth=self.monitor
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        before=self.path.read_bytes()
        url=f'http://127.0.0.1:{server.server_port}/api/paper-growth'
        try:
            with urllib.request.urlopen(url) as response:
                self.assertEqual(json.load(response)['scope'],'paper_by_strategy')
            with urllib.request.urlopen(urllib.request.Request(url,method='HEAD')) as response:
                self.assertEqual(response.read(),b'')
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(urllib.request.Request(url,method='POST',data=b'{}'))
            self.assertIn(error.exception.code,(404,405))
            self.assertEqual(self.path.read_bytes(),before)
        finally:
            server.shutdown();server.server_close();thread.join(2)


if __name__ == '__main__':
    unittest.main()
