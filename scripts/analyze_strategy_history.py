"""Read a frozen retirement backup; never mutate trading ledgers."""
from pathlib import Path
import collections
import datetime as dt
import html
import json
import math
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
BACKUP = Path((ROOT / 'logs/strategy-retirement-backup-path.txt').read_text().strip())
OUT = ROOT / 'reports/strategy-consolidation-20261026'
OUT.mkdir(parents=True, exist_ok=True)


def stats(rows):
    rows = sorted(rows, key=lambda r:r.get('settled_at') or r.get('created_at') or r.get('opened_at') or 0)
    pnl = [float(r.get('pnl') or 0) for r in rows]
    stake = sum(float(r.get('stake') or 0) for r in rows)
    wins = sum(p > 0 for p in pnl)
    gain, loss = sum(p for p in pnl if p > 0), -sum(p for p in pnl if p < 0)
    equity = peak = drawdown = 0
    for p in pnl:
        equity += p
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {'n': len(rows), 'pnl': round(sum(pnl), 4), 'stake': round(stake, 4),
            'turnover_roi_pct': round(sum(pnl) / stake * 100, 3) if stake else None,
            'win_pct': round(wins / len(rows) * 100, 2) if rows else None,
            'profit_factor': round(gain / loss, 4) if loss else None,
            'settled_pnl_drawdown': round(drawdown, 4),
            'pnl_without_top5': round(sum(pnl) - sum(sorted(pnl, reverse=True)[:5]), 4)}


ledgers, paper_rows, shadow, source_counts = [], {}, {}, collections.Counter()
official_outcomes = collections.defaultdict(set)
paths = sorted((BACKUP / 'databases').rglob('*.sqlite3'))
for p in paths:
    c = sqlite3.connect(p.as_uri() + '?mode=ro', uri=True)
    c.row_factory = sqlite3.Row
    tables = {r[0] for r in c.execute("select name from sqlite_master where type='table'")}
    if 'market_rounds' in tables and 'settlement_source' in {r[1] for r in c.execute('pragma table_info(market_rounds)')}:
        for r in c.execute("select round_id,outcome from market_rounds where settlement_source='polymarket_official' and outcome in ('Up','Down')"):
            official_outcomes[r['round_id']].add(r['outcome'])
    c.close()

for p in paths:
    c = sqlite3.connect(p.as_uri() + '?mode=ro', uri=True)
    c.row_factory = sqlite3.Row
    rel = str(p.relative_to(BACKUP / 'databases'))
    tables = {r[0] for r in c.execute("select name from sqlite_master where type='table'")}
    if 'trades' not in tables:
        c.close()
        continue
    all_rows = [dict(r) for r in c.execute('select * from trades order by opened_at,id')]
    rows = [r for r in all_rows if r['status'] == 'SETTLED']
    account = dict(c.execute('select * from account').fetchone())
    fees = c.execute('select coalesce(sum(fee),0) from paper_orders').fetchone()[0] if 'paper_orders' in tables else None
    canonical, disagreements, unknown = [], 0, 0
    for row in rows:
        if row.get('settlement_source') == 'early_exit':
            canonical.append(row)
            continue
        if row.get('settlement_source') not in ('polymarket_official','chainlink_fallback'):
            unknown += 1
            continue
        outcomes = official_outcomes.get(row['round_id'], set())
        if len(outcomes) != 1:
            unknown += 1
            continue
        correct = row['shares'] * (row['side'] in outcomes) - row['stake']
        if abs(correct - row['pnl']) > .0001:
            disagreements += 1
        canonical.append({**row, 'pnl': correct})
    summary = {'ledger': rel, 'account': {k: account[k] for k in ('initial_balance','cash_balance','realized_pnl')},
               'historical': stats(rows), 'canonical_official_or_exit': stats(canonical),
               'canonical_unavailable': unknown, 'settlement_disagreements': disagreements,
               'fees': round(fees, 4) if fees is not None else None,
               'open_positions': len(all_rows)-len(rows),
               'settlement_sources': dict(collections.Counter(r.get('settlement_source') or 'unknown' for r in rows))}
    ledgers.append(summary)
    if not rel.startswith('live/') or '_paper' in rel:
        paper_rows[rel] = canonical
    if 'aggressive_edge_v2_shadow_samples' in tables:
        count = 0
        for row in c.execute('select * from aggressive_edge_v2_shadow_samples order by id'):
            count += 1
            r = dict(row)
            key = (r['round_id'], r['sample_key'], r['side'])
            # Prefer the original common diagnostic pool, not a later version's backfill.
            if key not in shadow or p.name == 'single_fak_aggressive_edge_diagnostic.sqlite3':
                shadow[key] = r
        source_counts[rel] = count
    c.close()

main = paper_rows['polybot2other-real-btc.sqlite3']
main = [r for r in main if r['opened_at'] < dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc).timestamp()]
rounds = sorted({r['round_id'] for r in main}, key=lambda r: int(r.rsplit('-',1)[1]))
split = int(rounds[int(len(rounds) * .7)].rsplit('-',1)[1])
train = [r for r in main if r['opened_at'] < split]
holdout = [r for r in main if r['opened_at'] >= split]
bins = {}
for lo, hi in ((0,.2),(.2,.4),(.4,.55),(.55,.65),(.65,1.01)):
    bins[f'{lo:.2f}-{hi:.2f}'] = {name: stats([r for r in rr if lo <= r['entry_price'] < hi]) for name,rr in [('earlier',train),('later',holdout)]}

diagnostic = collections.defaultdict(list)
for r in shadow.values():
    outcomes = official_outcomes.get(r['round_id'],set())
    if len(outcomes) != 1 or not 0 < (r['entry_price'] or 0) < 1:
        continue
    # Descriptive hypothetical samples only: original features were overwritten in-place.
    price = r['entry_price']
    cost_per_share = price + .07 * price * (1-price)
    pnl = 2 / cost_per_share * (r['side'] in outcomes) - 2
    feature = json.loads(r['features_json'])
    base = {**r, 'pnl':pnl, 'stake':2}
    for label in ('all', 'side:'+r['side'], 'minute:'+r['sample_key'].split(':')[0],
                  'external_divergence:'+str(feature.get('external_divergence_count'))):
        diagnostic[label].append(base)

result = {'backup':str(BACKUP), 'ledger_count':len(ledgers), 'ledgers':ledgers,
          'shadow_raw_rows':sum(source_counts.values()),'shadow_unique_rows':len(shadow),
          'shadow_source_rows':dict(source_counts),'shadow_descriptive':{k:stats(v) for k,v in diagnostic.items()},
          'main_time_split_utc':dt.datetime.fromtimestamp(split,dt.timezone.utc).isoformat(),
          'main_earlier':stats(train),'main_later':stats(holdout),'main_price_bins':bins,
          'limitations':['Shadow UPSERT overwrites features without changing created_at; not a reliable tick replay.',
                         'Multiple observations share a market outcome and are not independent trades.',
                         'Canonical outcomes reconcile labels only; they cannot reconstruct a different historical cash path.',
                         'Historical filled-order filtering omits rejected/unobserved opportunities and is not a full backtest.',
                         'The new strategy requires independent forward Paper validation; no profitability claim.']}
(OUT/'analysis.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
print(json.dumps({k:result[k] for k in ('ledger_count','shadow_raw_rows','shadow_unique_rows','main_time_split_utc','main_earlier','main_later','main_price_bins','shadow_descriptive')},ensure_ascii=False))
