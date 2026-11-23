import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing

script = Path(__file__).resolve().parents[1] / "scripts/project_strong_signal_market.py"
spec = importlib.util.spec_from_file_location("strong_market_projection", script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class MarketProjectionTests(unittest.TestCase):
    def test_private_projection_streams_committed_ticks_across_writer_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp)/"server", Path(tmp)/"new-feed"
            main = module.writer(source/module.MAIN, (module.MARKET_TABLE, "CREATE TABLE balances(secret)"))
            upstream = module.writer(source/module.REF, (module.TICK_TABLE, module.VALIDATION_TABLE))
            self.addCleanup(main.close)
            t = 1_800_000_000
            ms = t * 1000
            values = (f"btc-updown-5m-{t}", "BTC", t, t+300, 65000, "q", "c", "u", "d", "s", "")
            with main:
                main.execute("INSERT INTO market_rounds VALUES(?,?,?,?,?,?,?,?,?,?,?)", values)
            with upstream:
                upstream.execute("INSERT INTO reference_ticks VALUES(?,?,?,?,?)",
                                 ("price.crypto", ms, 65001, ms+80, 0))
                upstream.execute("INSERT INTO reference_validation VALUES(?,?,?)", ("r1", t, 1))
            project = module.Projection(source, output)
            with closing(project):
                project.tick(t+.3)
                with closing(module.reader(output/module.MAIN)) as projected:
                    self.assertEqual(projected.execute("SELECT * FROM market_rounds").fetchone(), values)
                    self.assertIsNone(projected.execute("SELECT name FROM sqlite_master WHERE name='balances'").fetchone())
                upstream.close()
                with closing(sqlite3.connect(source/module.REF)) as restarted:
                    with restarted:
                        restarted.execute("INSERT INTO reference_ticks VALUES(?,?,?,?,?)",
                                          ("price.crypto", ms+1000, 65002, ms+1150, 1))
                        restarted.execute("INSERT INTO reference_validation VALUES(?,?,?)", ("r2", t+1, 0))
                project.tick(t+1.3)
                with closing(module.reader(output/module.REF)) as projected:
                    self.assertEqual(projected.execute("SELECT price,received_ms,snapshot FROM reference_ticks "
                                                       "ORDER BY source_ms DESC").fetchall(),
                                     [(65002, ms+1150, 1), (65001, ms+80, 0)])
                    self.assertEqual(projected.execute("SELECT verified FROM reference_validation "
                                                       "ORDER BY checked_at DESC LIMIT 1").fetchone()[0], 0)
            with closing(module.reader(source/module.MAIN)) as reader:
                with self.assertRaises(sqlite3.OperationalError):
                    reader.execute("UPDATE market_rounds SET target_price=0")

    def test_reject_source_as_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                module.Projection(Path(tmp), Path(tmp))
