from pathlib import Path
import bisect
import collections
import datetime as dt
import json
import math
import sqlite3
import statistics as st
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from polybot2other.adaptive_features import extract_market_features

TZ = ZoneInfo('Asia/Shanghai')
CUTOFF = dt.datetime(2026, 11, 18, 10, 53, 4, tzinfo=TZ).timestamp()
def epoch(s): return dt.datetime.fromisoformat(s).replace(tzinfo=TZ).timestamp()
WINDOWS = {'evening': (epoch('2026-11-17T18:00:00'), epoch('2026-11-18T00:00:00')),
           'night': (epoch('2026-11-18T00:00:00'), epoch('2026-11-18T06:00:00')),
           'morning': (epoch('2026-11-18T06:00:00'), CUTOFF)}
FILES = {'MAIN': 'polybot2other-real-btc.sqlite3', 'MAIN_OPTIMIZED': 'strategy-experiments/main_optimized.sqlite3',
         'MAIN_ADAPTIVE': 'strategy-experiments/main_adaptive.sqlite3',
         'MAIN_OPTIMIZED_FAST': 'strategy-experiments/main_optimized_fast.sqlite3',
         'MAIN_OPTIMIZED_PLUS': 'strategy-experiments/main_optimized_plus.sqlite3'}
def connect(path):
    c = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    c.row_factory = sqlite3.Row
    return c
def avg(v):
    v = [x for x in v if x is not None and math.isfinite(x)]
    return st.mean(v) if v else None
def median(v):
    v = [x for x in v if x is not None and math.isfinite(x)]
    return st.median(v) if v else None
def period(t): return next((k for k,(a,b) in WINDOWS.items() if a <= t < b), 'earlier')

rc = connect(ROOT / 'snapshot/live-accounts/market.references.sqlite3')
TICKS = [tuple(r) for r in rc.execute("SELECT source_ms,price,received_ms FROM reference_ticks WHERE channel='price.crypto' ORDER BY source_ms")]
TIMES = [r[0] for r in TICKS]
def history(at, seconds=3605):
    end = at * 1000
    return [r for r in TICKS[bisect.bisect_left(TIMES,end-seconds*1000):bisect.bisect_right(TIMES,end)] if r[2] <= end]

def path_stats(points, seconds):
    if not points: return {}
    end = points[-1][0]
    q = [r for r in points if r[0] >= end-seconds*1000]
    if len(q)<3 or (q[-1][0]-q[0][0])/1000 < seconds*.95: return {}
    changes = [(math.log(b[1]/a[1])*10000, (b[0]-a[0])/1000) for a,b in zip(q,q[1:]) if 0 < b[0]-a[0] <= 4000]
    elapsed = sum(d for _,d in changes)
    if elapsed < seconds*.90: return {}
    variance = sum(x*x for x,_ in changes)
    net = math.log(q[-1][1]/q[0][1])*10000
    absolute = sum(abs(x) for x,_ in changes)
    return {'momentum': net, 'sigma': math.sqrt(variance/elapsed), 'efficiency': abs(net)/absolute if absolute else 0,
            'coverage': elapsed/seconds, 'range_bps': math.log(max(r[1] for r in q)/min(r[1] for r in q))*10000}

records = []
for key, name in FILES.items():
    c = connect(ROOT / 'snapshot' / name)
    orders = {r['trade_id']:dict(r) for r in c.execute('SELECT p.*,s.payload FROM paper_orders p JOIN paper_sim_orders s ON s.order_id=p.id WHERE p.trade_id IS NOT NULL')}
    rows = c.execute("SELECT t.*,r.started_at,r.ends_at,r.outcome,r.target_price AS settlement_target,r.final_price,r.settled_at AS round_settled_at FROM trades t JOIN market_rounds r USING(round_id) WHERE t.status='SETTLED' AND t.settled_at<? ORDER BY t.opened_at,t.id", (CUTOFF,)).fetchall()
    for row in rows:
        r = dict(row); order = orders[r['id']]; p = json.loads(order['payload'])
        f = p.get('adaptive_decision', p.get('fast_decision',p.get('plus_decision',{})))
        decision_at = f.get('decision_at',p['submitted_at'])
        # Restrict reconstructed observations to information received by submission.
        cutoff = min(decision_at, p['submitted_at'])
        target = p['market']['target_price']; sign = 1 if r['side']=='Up' else -1
        points = history(cutoff)
        feature = extract_market_features(points, decision_at=cutoff, ask=f.get('ask',p['signal']['entry_price']), bid=f.get('bid'), target_price=target)
        r.update(strategy=key, decision_at=cutoff, decision_cst=dt.datetime.fromtimestamp(cutoff,TZ).isoformat(),
                 period=period(cutoff), settlement_period=period(r['settled_at']),
                 budget=order['requested_cash'], revision=p.get('revision'), won=r['pnl']>0,
                 fee=order['fee'], roi=r['pnl']/r['stake'], normalized_5=5*r['pnl']/r['stake'],
                 all_in_cost=r['stake']/r['shares'],
                 submission_all_in_cost=p['limit'] + p['info']['fee_rate']*p['limit']*(1-p['limit']),
                 signal_price=p['signal']['entry_price'], target_at_decision=target,
                 target_change_bps=(r['settlement_target']/target-1)*10000,
                 remaining_seconds=r['ends_at']-cutoff,
                 adaptive_state=f.get('adaptive_state'),adaptive_delta=f.get('adaptive_delta'),
                 adaptive_validation_state=f.get('adaptive_validation_state'),
                 adaptive_validation_reason=f.get('adaptive_validation_reason'),
                 source_age_seconds=(cutoff*1000-points[-1][0])/1000 if points else None,
                 feature_status=feature['v3_feature_status'],
                 feature_source_ms=feature['v3_feature_source_ms'],feature_received_ms=feature['v3_feature_received_ms'],
                 committed_feature_status=f.get('v3_feature_status'),
                 spread=f.get('ask',0)-f.get('bid',0) if f.get('ask') and f.get('bid') else None,
                 future_features_are_diagnostic_only=True)
        r.update({k:feature.get('v3_'+k+'_bps') for k in ['momentum_10','momentum_30','momentum_60','volatility','trend_gap']})
        r['submission_edge'] = r['confidence']-r['submission_all_in_cost']
        for sec in (120,300,900,3600):
            stats = path_stats(points,sec)
            for field in ('momentum','sigma','efficiency','range_bps','coverage'): r[f'{field}_{sec}']=stats.get(field)
        r['sigma_ratio_120_900'] = r['sigma_120']/r['sigma_900'] if r['sigma_900'] and r['sigma_120'] is not None else None
        if points:
            latest = points[-1]
            r['spot_price'] = latest[1]
            r['signed_target_gap_bps'] = sign*math.log(latest[1]/target)*10000
            r['momentum_30_aligned'] = sign*r['momentum_30'] if r['momentum_30'] is not None else None
            r['momentum_60_aligned'] = sign*r['momentum_60'] if r['momentum_60'] is not None else None
            r['momentum_300_aligned'] = sign*r['momentum_300'] if r['momentum_300'] is not None else None
            signs = [1 if q[1]>target else -1 for q in points if q[0] >= max(r['started_at']*1000,latest[0]-120000)]
            r['known_round_crossings'] = sum(a!=b for a,b in zip(signs,signs[1:]))
            sigma = max(.5,r['sigma_120']) if r['sigma_120'] is not None else None
            r['sigma_model'] = sigma
            r['target_distance_z'] = r['signed_target_gap_bps']/(sigma*math.sqrt(max(.01,r['ends_at']-latest[0]/1000-40))) if sigma else None
        else:
            for field in ('spot_price','signed_target_gap_bps','momentum_30_aligned','momentum_60_aligned','momentum_300_aligned','known_round_crossings','sigma_model','target_distance_z'):r[field]=None
        # Outcomes and future path are for explanation only, never input to a causal rule.
        future = [q for q in TICKS[bisect.bisect_left(TIMES,cutoff*1000):bisect.bisect_right(TIMES,r['ends_at']*1000)] if q[2]<=r['ends_at']*1000+5000]
        changes=[(math.log(b[1]/a[1])*10000,(b[0]-a[0])/1000) for a,b in zip(future,future[1:]) if 0 < b[0]-a[0] <= 4000]
        elapsed=sum(t for _,t in changes)
        r['future_sigma'] = math.sqrt(sum(v*v for v,_ in changes)/elapsed) if elapsed >= r['remaining_seconds']*.9 else None
        r['future_to_model_sigma'] = r['future_sigma']/r['sigma_model'] if r['future_sigma'] is not None and r['sigma_model'] else None
        r['future_final_margin_bps'] = sign*math.log(r['final_price']/target)*10000 if r['final_price'] and r['final_price']>0 else None
        records.append(r)

FEATURES = ['confidence','entry_price','all_in_cost','submission_edge','remaining_seconds','source_age_seconds',
            'volatility','sigma_120','sigma_900','sigma_3600','sigma_ratio_120_900','sigma_model','target_distance_z',
            'signed_target_gap_bps','momentum_30_aligned','momentum_60_aligned','momentum_300_aligned',
            'efficiency_120','efficiency_300','efficiency_900','known_round_crossings','spread',
            'future_sigma','future_to_model_sigma','future_final_margin_bps']
def summarize(rows):
    return {'n':len(rows),'unique_rounds':len(set(r['round_id'] for r in rows)),
            'wins':sum(r['won'] for r in rows),'win_rate':avg([r['won'] for r in rows]),
            'pnl':sum(r['pnl'] for r in rows),'fees':sum(r['fee'] for r in rows),
            'cost':sum(r['stake'] for r in rows),'roi_per_trade':avg([r['roi'] for r in rows]),
            'normalized_5':sum(r['normalized_5'] for r in rows),
            'features':{k:{'n':sum(r.get(k) is not None for r in rows),'mean':avg([r.get(k) for r in rows]),'median':median([r.get(k) for r in rows])} for k in FEATURES}}
summary={'cutoff':CUTOFF,'windows':WINDOWS,'by_decision_period':{},'by_settlement_period':{},'adaptive_states':{},'checks':{}}
for field,target in [('period','by_decision_period'),('settlement_period','by_settlement_period')]:
    for name in ['earlier',*WINDOWS]:
        selected=[r for r in records if r[field]==name]
        summary[target][name]={'all':summarize(selected),**{k:summarize([r for r in selected if r['strategy']==k]) for k in FILES}}
for name in ['earlier',*WINDOWS]:
    subset=[r for r in records if r['period']==name and r['strategy']=='MAIN_ADAPTIVE']
    summary['adaptive_states'][name]={field:dict(collections.Counter(str(r[field]) for r in subset)) for field in ['adaptive_state','adaptive_delta','adaptive_validation_state','adaptive_validation_reason']}
summary['checks']={'all_targets_equal_to_settlement':all(abs(r['target_change_bps'])<.001 for r in records),
                   'future_received_features':sum(r['feature_received_ms'] is not None and r['feature_received_ms']>r['decision_at']*1000 for r in records),
                   'feature_statuses':dict(collections.Counter(r['feature_status'] for r in records)),
                   'distinct_markets':len(set(r['round_id'] for r in records))}
(ROOT/'trade-features.json').write_text(json.dumps(records,ensure_ascii=False,indent=2))
(ROOT/'regime-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
print('checks',json.dumps(summary['checks'],ensure_ascii=False))
for name in WINDOWS:
    s=summary['by_decision_period'][name]['all']
    print(name,json.dumps({k:s[k] for k in ['n','unique_rounds','wins','win_rate','pnl','normalized_5','roi_per_trade']},ensure_ascii=False))
    print('features',json.dumps({k:round(v['mean'],4) if v['mean'] is not None else None for k,v in s['features'].items()},ensure_ascii=False))
print('adaptive_states',json.dumps(summary['adaptive_states'],ensure_ascii=False))
