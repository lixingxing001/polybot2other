"""Reproduce V1.1's historical score reference without changing any trading ledger."""
from pathlib import Path
import datetime as dt
import hashlib
import json
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT/'data/backups/strategy-retirement-20261026-225605/databases/polybot2other-real-btc.sqlite3'
OUTPUT = ROOT/'reports/value-confirm-participation-20261026'
OUTPUT.mkdir(parents=True,exist_ok=True)
c=sqlite3.connect(SOURCE.as_uri()+'?mode=ro',uri=True)
c.row_factory=sqlite3.Row
rows=[dict(r) for r in c.execute("""
    select t.*,m.ends_at from trades t join market_rounds m using(round_id)
    where t.status='SETTLED' and t.settlement_source='polymarket_official'
      and t.opened_at < 1790812800 order by t.opened_at,t.id
""")]
c.close()
markets=sorted({r['round_id'] for r in rows},key=lambda x:int(x.rsplit('-',1)[1]))
cut=int(markets[int(.7*len(markets))].rsplit('-',1)[1])
earlier=[r for r in rows if r['opened_at']<cut]
later=[r for r in rows if r['opened_at']>=cut]
bins=[]
for lo,hi in ((.6,.7),(.7,.8),(.8,.9),(.9,1.001)):
    group=[r for r in earlier if lo<=r['confidence']<hi]
    wins=sum(r['pnl']>0 for r in group)
    bins.append({'score_min':lo,'score_max_exclusive':hi,'n':len(group),'wins':wins,
                 'reference_probability':(wins+10)/(len(group)+20)})
def availability(rr):
    seen=set()
    for r in rr:
        reference=next((b['reference_probability'] for b in bins if b['score_min']<=r['confidence']<b['score_max_exclusive']),None)
        price=r['entry_price'];limit=min(.65,price+.01)
        if reference is not None and .2<=price<=.65 and 30<=r['ends_at']-r['opened_at']<=270 and reference-limit-.07*limit*(1-limit)>=.03:
            seen.add(r['round_id'])
    return {'price_time_cost_eligible_markets':len(seen),'observed_markets':len({r['round_id'] for r in rr})}
result={'policy_version':'20261026-calibrated-v1.1','source':str(SOURCE),
        'source_sha256':hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
        'training_cutoff_utc':dt.datetime.fromtimestamp(cut,dt.timezone.utc).isoformat(),
        'prior_wins':10,'prior_losses':10,'bins':bins,
        'earlier_availability':availability(earlier),'later_availability':availability(later),
        'limits':['Historical filled orders are selected observations, not all market opportunities.',
                  'Earlier and later samples have already been inspected; neither is a new independent validation.',
                  'Counts omit live momentum, external confirmation, order depth and funding constraints.',
                  'Historical reference probabilities are not guarantees and are not automatically refitted.']}
(OUTPUT/'calibration.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
print(json.dumps(result,ensure_ascii=False,indent=2))
