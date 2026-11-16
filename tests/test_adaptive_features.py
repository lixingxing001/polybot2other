"""V3 optional features are based on source time and actual local receipt time."""
from hashlib import sha256
import math
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import threading
import unittest

from polybot2other.adaptive_features import (
    MARKET_FIELDS, extract_market_features, read_reference_features, current_reference_features,
)

NOW = 10000.


def points(price=lambda i:100*math.exp(i/10000)):
    return [(int((NOW-120+i)*1000),price(i),int((NOW-120+i)*1000)+100) for i in range(121)]


class AdaptiveFeatureTests(unittest.TestCase):
    def test_future_source_or_late_received_ticks_are_excluded(self):
        normal=points()
        future=[(int((NOW+1)*1000),99999,int(NOW*1000)),
                (int(NOW*1000),99999,int((NOW+10)*1000)),
                (int((NOW-1)*1000),99999,int((NOW+10)*1000))]
        before=extract_market_features(normal,decision_at=NOW+.2)
        after=extract_market_features(normal+future,decision_at=NOW+.2)
        self.assertEqual(before,after)
        self.assertLessEqual(after['v3_feature_source_ms'],(NOW+.2)*1000)
        self.assertLessEqual(after['v3_feature_received_ms'],(NOW+.2)*1000)
        self.assertEqual(after['v3_feature_status'],'READY')

    def test_feature_momentum_and_volatility_have_known_units(self):
        result=extract_market_features(points(),decision_at=NOW+.2,ask=.6,bid=.58)
        for period in (10,30,60):self.assertAlmostEqual(result[f'v3_momentum_{period}_bps'],period)
        self.assertAlmostEqual(result['v3_volatility_bps'],1)
        self.assertAlmostEqual(result['v3_spread'],.02)

    def test_trend_uses_time_weighted_price_not_point_count(self):
        # 30 seconds at 100 then 30 seconds at 110. Sampling density changes
        # halfway through; a simple mean of observed ticks would be wrong.
        rows=[]
        for second in range(-120,1):
            if second < -30 and second%2:continue
            price=100 if second < -30 else 110
            stamp=int((NOW+second)*1000)
            rows.append((stamp,price,stamp+100))
        result=extract_market_features(rows,decision_at=NOW+.2)
        self.assertEqual(result['v3_feature_status'],'READY')
        self.assertAlmostEqual(result['v3_trend_gap_bps'],math.log(110/105)*10000)

    def test_missing_stale_and_gapped_history_do_not_fabricate_zero_motion(self):
        missing=extract_market_features([],decision_at=NOW,ask=.6,bid=.59)
        self.assertEqual(missing['v3_feature_status'],'MISSING_HISTORY')
        self.assertIsNone(missing['v3_momentum_10_bps'])
        self.assertAlmostEqual(missing['v3_spread'],.01)
        stale=extract_market_features(points(),decision_at=NOW+4)
        self.assertEqual(stale['v3_feature_status'],'STALE_HISTORY')
        self.assertIsNone(stale['v3_volatility_bps'])
        rows=[r for r in points() if not (NOW-50)*1000 < r[0] < (NOW-40)*1000]
        gapped=extract_market_features(rows,decision_at=NOW+.2)
        self.assertEqual(gapped['v3_feature_status'],'GAPPED_HISTORY')
        for field in MARKET_FIELDS[:-1]:self.assertIsNone(gapped[field])

    def test_duplicate_source_tick_uses_earliest_available_receipt(self):
        rows=points()
        duplicate=(rows[-1][0],99999,rows[-1][2]+50)
        self.assertEqual(extract_market_features(rows,decision_at=NOW+.2),
                         extract_market_features(rows+[duplicate],decision_at=NOW+.2))

    def test_read_only_feature_recovery_preserves_source_and_handles_absence(self):
        with TemporaryDirectory() as temporary:
            path=Path(temporary)/'reference.sqlite3'
            conn=sqlite3.connect(path)
            conn.execute('CREATE TABLE reference_ticks(channel TEXT,source_ms INTEGER,price REAL,received_ms REAL)')
            conn.executemany('INSERT INTO reference_ticks VALUES(?,?,?,?)',
                [('price.crypto',*row) for row in points()])
            conn.commit();conn.close()
            before=sha256(path.read_bytes()).digest()
            result=read_reference_features(path,NOW+.2)
            self.assertEqual(result['v3_feature_status'],'READY')
            self.assertEqual(before,sha256(path.read_bytes()).digest())
            absent=Path(temporary)/'missing.sqlite3'
            self.assertEqual(read_reference_features(absent,NOW)['v3_feature_status'],'MISSING_HISTORY')
            self.assertFalse(absent.exists())
            conn=sqlite3.connect(path)
            refs=SimpleNamespace(conn=conn,lock=threading.RLock())
            self.assertEqual(current_reference_features(refs,decision_at=NOW+.2),result)
            conn.close()

    def test_invalid_optional_timestamp_returns_missing_instead_of_stopping(self):
        result=current_reference_features(SimpleNamespace(),decision_at=None)
        self.assertEqual(result['v3_feature_status'],'MISSING_HISTORY')
        for field in MARKET_FIELDS:self.assertIsNone(result[field])
