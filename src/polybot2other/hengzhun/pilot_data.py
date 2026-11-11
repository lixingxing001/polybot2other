"""Observed-data bootstrap for an explicitly uncalibrated Paper experiment.

No labels are invented and no original landmark is changed. Seed prices keep
their original source/receipt clocks. Historical returns never bridge a gap.
"""
from bisect import bisect_left, bisect_right
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import statistics

from .config import load_config
from .contracts import canonical, digest
from .model import (PriceObservation, ProxyEstimate, ModelUnavailable, causal_grid,
                    estimate_volatility, integrate_spot, predict_terminal_twap, linear_quantile)

SPOT = 'price.crypto'
TWAP = 'price.crypto.twap'


def build_seed(source, destination, cutoff):
    """Freeze a small read-only extraction; never copy or rewrite the raw DB."""
    source, destination = Path(source), Path(destination)
    if destination.exists():
        raise ValueError('SEED_ALREADY_EXISTS')
    destination.mkdir(parents=True)
    old = sqlite3.connect(source.resolve().as_uri()+'?mode=ro', uri=True, timeout=10)
    old.execute('BEGIN')
    try:
        rows = old.execute('SELECT channel,source_at,received_at,value,event_id FROM price_observations '
                           'WHERE source_at<=? AND received_at<=? ORDER BY channel,source_at', (cutoff, cutoff)).fetchall()
        counts = {key: old.execute('SELECT count(*) FROM '+key).fetchone()[0]
                  for key in ['market_contracts','official_results','landmarks']}
        states = dict(old.execute('SELECT status,count(*) FROM landmarks GROUP BY status'))
        run_id = old.execute("SELECT value FROM capture_meta WHERE key='run_id'").fetchone()[0]
    finally:
        old.close()
    seed = sqlite3.connect(destination/'prices.sqlite3')
    with seed:
        seed.execute('CREATE TABLE prices(channel TEXT,source_at INTEGER,received_at INTEGER,value TEXT,event_id TEXT,PRIMARY KEY(channel,source_at))')
        seed.executemany('INSERT INTO prices VALUES(?,?,?,?,?)', rows)
    seed.close()
    spot = [PriceObservation(int(s), int(r), float(v)) for ch,s,r,v,_ in rows if ch == SPOT]
    stamps = [p.source_at_ms for p in spot]
    errors=[];last_sample=-60000
    for ch,s,r,v,_ in rows:
        if ch != TWAP or s <= cutoff-86400000 or s-last_sample < 60000:
            continue
        subset = spot[max(0,bisect_left(stamps,s-62000)-1):bisect_right(stamps,s)]
        try:
            integral = integrate_spot(subset,s-60000,s,r,max_gap_ms=2000)
            visible = [p for p in subset if p.source_at_ms <= s and p.received_at_ms <= r]
            anchor = visible[-1]
            if s-anchor.source_at_ms > 2000:
                continue
            errors.append({'source_at':s,'available_at':r,'error_bps':(float(v)-integral/60)/anchor.value*10000})
            last_sample=s
        except (ValueError,IndexError):
            continue
    # Source-time returns are a frozen historic fallback, not causal landmark
    # features or a claim of fitted/validated predictive accuracy.
    by_source={p.source_at_ms:p.value for p in spot if p.source_at_ms>cutoff-86400000}
    returns=[math.log(value/by_source[s-5000]) for s,value in by_source.items()
             if s%5000==0 and s-5000 in by_source and all(s-k*1000 in by_source for k in range(6))]
    if not returns:
        raise ValueError('NO_USABLE_HISTORICAL_RETURNS')
    values=[p['error_bps'] for p in errors]
    bias=statistics.median(values) if values else 0.
    sd=max(1.,math.sqrt(sum((v-bias)**2 for v in values)/len(values))) if values else 1.
    manifest={'kind':'HENGZHUN_EXISTING_DATA_PILOT','created_at':cutoff,'source_database':str(source.resolve()),
              'source_run_id':run_id,'source_counts':counts,'original_landmarks':states,
              'price_rows':len(rows),'spot_rows':len(spot),'twap_rows':sum(r[0]==TWAP for r in rows),
              'source_start_at':min(r[1] for r in rows),'source_end_at':max(r[1] for r in rows),
              'price_rows_sha256':digest(rows),'seed_file_sha256':hashlib.sha256((destination/'prices.sqlite3').read_bytes()).hexdigest(),
              'historical_return_count':len(returns),'historical_log_variance_per_second':sum(v*v for v in returns)/(5*len(returns)),
              'proxy_sample_count':len(errors),'proxy_bias_bps':bias,'proxy_sd_bps':sd,
              'proxy_abs_error_p95_bps':linear_quantile([abs(v) for v in values],.95) if values else None,
              'model_status':'UNFITTED','predictive_calibration':'NOT_PERFORMED',
              'sample_scope':'Existing observed prices only; original formal training/validation gates remain unpassed.'}
    manifest['manifest_sha256']=digest(manifest)
    (destination/'manifest.json').write_text(canonical(manifest))
    (destination/'proxy-errors.json').write_text(canonical(errors))
    return manifest


class PilotPrior:
    def __init__(self, seed, at_ms):
        self.seed=Path(seed)
        self.manifest=json.loads((self.seed/'manifest.json').read_text())
        check=dict(self.manifest);saved=check.pop('manifest_sha256')
        if digest(check)!=saved or hashlib.sha256((self.seed/'prices.sqlite3').read_bytes()).hexdigest()!=self.manifest['seed_file_sha256']:
            raise ValueError('SEED_HASH_MISMATCH')
        if self.manifest['created_at']>at_ms:
            raise ValueError('SEED_FROM_FUTURE')
        self.observations={};self.cache_key=None;self.cache=None
        with sqlite3.connect((self.seed/'prices.sqlite3').resolve().as_uri()+'?mode=ro',uri=True) as c:
            for s,r,v in c.execute('SELECT source_at,received_at,value FROM prices WHERE channel=? AND source_at>=? AND received_at<=? ORDER BY source_at',
                                   (SPOT,at_ms-750000,at_ms)):
                self.add(int(s),int(r),float(v))
        m=self.manifest
        self.proxy=ProxyEstimate(m['proxy_bias_bps'],m['proxy_sd_bps'],m['proxy_sample_count'],m['proxy_abs_error_p95_bps'],False,'EXPERIMENTAL_OBSERVED_PROXY')
        self.config=load_config()

    def add(self, source, received, value):
        p=PriceObservation(source,received,value)
        old=self.observations.get(source)
        if old and old.value!=p.value:
            raise ModelUnavailable('DATA_CONFLICT')
        if not old or p.received_at_ms<old.received_at_ms:
            self.observations[source]=p

    def predict(self, market, spot, now):
        source=int(spot['source_ms']);received=int(spot['received_ms'])
        if not 0<=now-source<=3000 or not 0<=now-received<=2000:
            raise ModelUnavailable('PRICE_STALE')
        baseline=float(market['target_price'])
        if baseline<=0 or not math.isfinite(baseline):
            raise ModelUnavailable('BASELINE_MISSING')
        self.add(source,received,float(spot['price']))
        self.observations={s:p for s,p in self.observations.items() if s>=now-780000}
        key=(market['condition_id'],source,baseline)
        if key==self.cache_key:
            return dict(self.cache,decision_at=now,tau_wall_seconds=(market['ends_at']-now)/1000)
        points=sorted(self.observations.values(),key=lambda p:p.source_at_ms)
        grid=causal_grid(points,source//5000*5000-600000,source,source_max_age_ms=3000,receive_max_age_ms=2500)
        volatility_source='LIVE_CAUSAL_120_600_SECONDS';counts=None
        try:
            vol=estimate_volatility(grid,source,float(spot['price']))
            if vol.shock_cooldown_until_ms and now<vol.shock_cooldown_until_ms:
                raise ModelUnavailable('SHOCK_COOLDOWN')
            variance=vol.sigma_squared;counts={'fast':vol.fast_count,'slow':vol.slow_count}
        except ModelUnavailable as exc:
            if exc.reason_code!='VOL_WARMUP':raise
            volatility_source='FROZEN_EXISTING_DATA_VARIANCE'
            variance=float(spot['price'])**2*max(self.manifest['historical_log_variance_per_second'],(.25/10000)**2)
        known=0.
        if market['ends_at']-source<60000:
            known=integrate_spot(points,market['ends_at']-60000,source,now,max_gap_ms=2000)
        prediction=predict_terminal_twap(float(spot['price']),baseline,market['ends_at'],source,now,variance,known,self.proxy)
        result={**asdict(prediction),'decision_at':now,'source_at':source,'received_at':received,
                'baseline':baseline,'spot':float(spot['price']),'known_integral':known,'sigma_squared':variance,
                'volatility_source':volatility_source,'return_counts':counts,'probability_basis':'UNCALIBRATED_PRIOR',
                'seed_manifest_sha256':self.manifest['manifest_sha256'],'proxy':asdict(self.proxy)}
        self.cache_key=key;self.cache=result
        return result
