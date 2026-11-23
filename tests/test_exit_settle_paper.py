import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

from polybot2other.exit_settle_paper import ExitSettlePaper, VARIANT_ID
from polybot2other.paper_growth import PaperGrowthMonitor
from polybot2other.strategy_league import LeagueEngine
from polybot2other.web import DashboardServer, Handler


class ExitSettlePaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "strategy-league.sqlite3"
        self.bot = SimpleNamespace(store=SimpleNamespace(db_path=self.root/"main.sqlite3"),
                                   settings=SimpleNamespace(strategy_league_enabled=True))
        self.reader = ExitSettlePaper(self.bot)
        self.start = int(time.time()//300)*300-86400*2

    def create_ledger(self):
        engine = LeagueEngine(self.path, start_at=self.start)
        self.addCleanup(engine.close)
        con = engine.db
        with con:
            for i in range(3):
                market = f"btc-updown-5m-{self.start+300*i}"
                at = self.start+300*i
                con.execute("""INSERT INTO league_rounds VALUES(?,?,?,?,?,?,?,?,?)""",
                            (market,at,at+300,at,at+300,at+200,'Up' if i<2 else None,at+301 if i<2 else None,0))
                side = 'Down' if i==1 else 'Up'
                close = at+310 if i<2 else None
                pnl = (4.9,-4.1,0)[i]
                cash = (4.9,4.1,4.1)[i]
                acquired = 9.8 if i == 0 else 10
                con.execute("""INSERT INTO league_positions(strategy_id,round_id,side,opened_at,closed_at,
                    shares,stake,bought_shares,cost,proceeds,pnl,fees) VALUES('exit_settle',?,?,?,?,?,?,?,?,?,?,?)""",
                            (market,side,at+21,close,10 if i==2 else 0,cash if i==2 else 0,acquired,cash,acquired if i==0 else 0,pnl,.1))
                if close is not None:
                    con.execute("INSERT INTO league_pnl_events VALUES('exit_settle',?,?,?)",(market,close,pnl))
                levels = [{"price":round((cash-.1)/acquired,4),"shares":acquired,"notional":round(cash-.1,4),"fee":.1,"cash_spent":cash}]
                for key,stat in [('exit_settle','FILLED'), ('control_060','CANCELLED')]:
                    con.execute("""INSERT INTO league_orders(strategy_id,round_id,side,action,limit_price,submitted_at,
                       matched_at,finished_at,status,filled_shares,fee,cash,reason,token,source_ms,received_ms,notional_budget,
                       requested_shares,tick_size,min_size,fee_rate,fee_exponent,fills)
                       VALUES(?,?,?,'BUY',.6,?,?,?,?,?,?,?,?,?,1,1,4.8,0,'.01',5,.07,1,?)""",
                                (key,market,side,at+20,at+21,at+21,stat,acquired if key=='exit_settle' else 0,
                                 .1 if key=='exit_settle' else 0,cash if key=='exit_settle' else 0,'方向足够','token',json.dumps(levels)))
            con.execute("""UPDATE league_accounts SET cash=96.7,equity=101.7,realized=.8,max_drawdown=4.4
                           WHERE strategy_id='exit_settle'""")
            con.execute("UPDATE league_accounts SET realized=-1 WHERE strategy_id='control_060'")
        return con

    def test_original_wallet_current_valuation_and_different_realized_curve(self):
        con = self.create_ledger()
        before = con.total_changes
        view = self.reader.snapshot(1,1)
        v = view['variant']
        m = v['metrics']
        self.assertEqual((m['initial_balance'],m['total_equity'],m['realized_pnl'],m['unrealized_pnl'],m['max_drawdown']),
                         (100,101.7,.8,.9,4.4))
        self.assertEqual((v['stake_dollars'],v['excess_vs_control'],m['fees'],v['recent_trades_summary']['settled_count']),
                         (5.,1.8,.3,2))
        self.assertEqual(len(v['open_trades']),1)
        self.assertEqual(view['recent_trades_page']['recent_trades_meta']['total'],3)
        chart = self.reader.equity_curve_window(1,1200,full_history=True)
        self.assertEqual([p['total_equity'] for p in chart['equity_curve']],[100,104.9,100.8])
        self.assertEqual(chart['equity_curve'][0]['created_at'],self.start)
        self.assertEqual(chart['equity_curve_meta']['curve_basis'],'realized')
        self.assertEqual(self.reader.equity_curve_window(1,2)['equity_curve'][0]['total_equity'],100.8)
        self.assertEqual(before,con.total_changes)
        with con:
            con.execute("UPDATE league_accounts SET unknown_shares=10,equity=cash WHERE strategy_id='exit_settle'")
        m = self.reader.snapshot()['variant']['metrics']
        self.assertTrue(m['valuation_missing'])
        self.assertIsNone(m['total_equity'])
        self.assertIsNone(m['unrealized_pnl'])
        self.assertEqual(m['realized_pnl'],.8)

    def test_web_scope_paging_filters_fills_and_detail_dates(self):
        self.create_ledger()
        srv = DashboardServer(('127.0.0.1',0),Handler,self.bot)
        thread = threading.Thread(target=srv.serve_forever,kwargs={"poll_interval":.01},daemon=True)
        thread.start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        def get(path):
            conn = http.client.HTTPConnection(*srv.server_address,timeout=2)
            try:
                conn.request('GET',path)
                with conn.getresponse() as response:
                    payload = json.load(response)
                    self.assertEqual(response.status,200,payload)
                    return payload
            finally:
                conn.close()
        args = "account_scope=strategy_experiment&variant_id="+VARIANT_ID
        self.assertEqual(get('/api/exit-settle-paper')['variant']['variant_id'],VARIANT_ID)
        detail = get('/api/strategy-experiments?variant_id='+VARIANT_ID+'&trade_limit=1')
        self.assertEqual(len(detail['recent_trades_page']['recent_trades']),1)
        history = get('/api/recent-trades?'+args+'&limit=1&offset=1')
        self.assertEqual(history['recent_trades'][0]['pnl'],-4.1)
        closed = get('/api/recent-trades?'+args+'&end_at='+str(self.start+315))
        self.assertEqual(closed['recent_trades_summary']['total_count'],1)
        pending = get('/api/orders?'+args+'&status=active')
        self.assertEqual(pending['recent_orders_meta']['total'],0)
        order = get('/api/orders?'+args+'&status=filled&limit=1')['recent_orders'][0]
        self.assertEqual(order['avg_fill_price'],.4)
        fills = get('/api/order-fills?'+args+'&order_id='+str(order['id']))['fills']
        self.assertEqual((fills[0]['shares'],fills[0]['fee']),(10,.1))
        self.assertEqual(get('/api/order-fills?'+args+'&order_id='+str(order['id']+1))['fills'],[])
        self.assertEqual(get('/api/equity-curve?'+args+'&days=1&full_history=1')['equity_curve'][0]['total_equity'],100.)

    def test_growth_reads_settlements_only_and_missing_account_creates_no_file(self):
        self.assertFalse(self.reader.snapshot()['available'])
        self.assertFalse(self.path.exists())
        self.create_ledger()
        monitor = PaperGrowthMonitor(self.bot,clock=lambda:self.start+1000)
        g = next(r for r in monitor.snapshot()['strategies'] if r['strategy_id']==VARIANT_ID)
        self.assertTrue(g['available'])
        self.assertEqual(g['windows']['last1h']['trades'],2)
        self.assertEqual(g['windows']['last1h']['pnl'],.8)
