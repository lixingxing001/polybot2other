from pathlib import Path
import sqlite3,json,statistics,collections,datetime,csv,math
from zoneinfo import ZoneInfo
ROOT=Path(__file__).resolve().parent
SNAP=ROOT/'snapshot'
TZ=ZoneInfo('Asia/Shanghai')
FILES={'MAIN':'polybot2other-real-btc.sqlite3','MAIN_OPTIMIZED':'strategy-experiments/main_optimized.sqlite3','MAIN_ADAPTIVE':'strategy-experiments/main_adaptive.sqlite3','MAIN_OPTIMIZED_FAST':'strategy-experiments/main_optimized_fast.sqlite3','MAIN_OPTIMIZED_PLUS':'strategy-experiments/main_optimized_plus.sqlite3'}
def stamp(t):return datetime.datetime.fromtimestamp(t,TZ).isoformat(timespec='seconds') if t is not None else None
def mean(a):return statistics.mean(a) if a else None
def stats(rows):
 w=[r for r in rows if r['pnl']>0];l=[r for r in rows if r['pnl']<0]
 aw=mean([r['pnl'] for r in w]);al=mean([-r['pnl'] for r in l]);stake=sum(r['stake'] for r in rows);shares=sum(r['shares'] for r in rows)
 return dict(n=len(rows),wins=len(w),losses=len(l),win_rate=len(w)/len(rows) if rows else None,pnl=sum(r['pnl'] for r in rows),fees=sum(r['fee'] for r in rows),gross_pnl=sum(r['pnl']+r['fee'] for r in rows),stake=stake,roi=sum(r['pnl'] for r in rows)/stake if stake else None,avg_win=aw,avg_loss=al,empirical_breakeven=al/(aw+al) if aw and al else None,profit_factor=sum(r['pnl'] for r in w)/sum(-r['pnl'] for r in l) if l else None,avg_entry=mean([r['entry_price'] for r in rows]),avg_all_in_cost=mean([r['stake']/r['shares'] for r in rows]),weighted_all_in_cost=stake/shares if shares else None,avg_score=mean([r['confidence'] for r in rows]),model_expected_pnl=sum(r['confidence']*r['shares']-r['stake'] for r in rows),avg_time_left=mean([r['time_left'] for r in rows]),avg_slippage=mean([r['slippage'] for r in rows if r['slippage'] is not None]),avg_arrival_seconds=mean([r['arrival_seconds'] for r in rows if r['arrival_seconds'] is not None]))
def group(rows,key):
 d=collections.defaultdict(list)
 for r in rows:d[str(key(r))].append(r)
 return {k:stats(v) for k,v in sorted(d.items())}
def band(x,cuts):
 for i,(lo,hi) in enumerate(zip(cuts,cuts[1:])):
  if lo<=x<hi:return f'{lo:g}-{hi:g}'
 return 'other'
allrows={};result={'snapshot':json.loads((SNAP/'manifest.json').read_text()),'strategies':{}}
for key,file in FILES.items():
 c=sqlite3.connect((SNAP/file).resolve().as_uri()+'?mode=ro',uri=True);c.row_factory=sqlite3.Row
 acct=dict(c.execute('select * from account').fetchone());run=dict(c.execute('select * from paper_sim_run').fetchone());orders={x['id']:dict(x) for x in c.execute('select * from paper_orders')};payloads={x['order_id']:json.loads(x['payload']) for x in c.execute('select * from paper_sim_orders')}
 bytrade={}
 for oid,o in orders.items():
  if o['trade_id'] is not None:bytrade[o['trade_id']]=(o,payloads.get(oid,{}))
 rows=[dict(x) for x in c.execute('select t.*,r.started_at,r.ends_at,r.outcome,r.target_price,r.final_price,r.settlement_source as round_source,r.settled_at as round_settled_at from trades t join market_rounds r using(round_id) order by t.opened_at,t.id')]
 for r in rows:
  o,p=bytrade.get(r['id'],({},{}));f=c.execute('select coalesce(sum(fee),0) from paper_fills where trade_id=?',(r['id'],)).fetchone()[0];r.update(fee=f,requested_cash=o.get('requested_cash'),config_revision=p.get('revision'),time_left=r['ends_at']-r['opened_at'],slippage=r['entry_price']-p.get('signal',{}).get('entry_price',r['entry_price']),signal_price=p.get('signal',{}).get('entry_price'),arrival_seconds=p.get('matched_at',r['opened_at'])-p['submitted_at'] if p.get('submitted_at') else None,confirmation_seconds=p.get('confirmed_at',r['opened_at'])-p.get('matched_at',r['opened_at']),model_features=p.get('plus_decision',p.get('fast_decision',p.get('adaptive_decision',{}))),submitted_at=p.get('submitted_at'),order_id=o.get('id'),fee_rate=p.get('info',{}).get('fee_rate'),fee_exponent=p.get('info',{}).get('fee_exponent'))
  r['opened_cst']=stamp(r['opened_at']);r['settled_cst']=stamp(r['settled_at']);r['day']=r['opened_cst'][:10]
 settled=[r for r in rows if r['status']=='SETTLED'];opened=[r for r in rows if r['status']=='OPEN'];allrows[key]=rows
 equity=peak=acct['initial_balance'];dd=ddpct=0;streak=maxstreak=0;worst=[]
 for r in sorted(settled,key=lambda r:(r['settled_at'],r['id'])):
  equity+=r['pnl'];peak=max(peak,equity);dd=max(dd,peak-equity);ddpct=max(ddpct,(peak-equity)/peak);streak=streak+1 if r['pnl']<0 else 0;maxstreak=max(maxstreak,streak)
 current=sum(r['pnl'] for r in settled)+acct['initial_balance'];pending=c.execute('select coalesce(sum(amount),0) from paper_sim_claims where released_at is null').fetchone()[0];reserved=c.execute("select coalesce(sum(remaining_cash),0) from paper_orders where status in ('SUBMITTED','PARTIAL','OPEN','IN_FLIGHT','CONFIRMING')").fetchone()[0]
 policy=json.loads(run['policy_json']);events=list(c.execute('select * from paper_sim_events'))
 event_counts=collections.Counter(r['code'] for r in events);messages=collections.Counter((r['code'],r['message']) for r in events)
 rec={'account':acct,'run_started_cst':stamp(run['started_at']),'last_trade_cst':stamp(max(r['opened_at'] for r in rows)) if rows else None,'settled':stats(settled),'open':[{k:r[k] for k in ['id','round_id','side','stake','shares','entry_price','opened_cst','ends_at']} for r in opened],'open_risk':sum(r['stake'] for r in opened),'book_equity':current,'settlement_sources':dict(collections.Counter(r['settlement_source'] for r in settled)),'max_realized_drawdown':dd,'max_realized_drawdown_pct':ddpct,'max_loss_streak':maxstreak,'days':group(settled,lambda r:r['day']),'sides':group(settled,lambda r:r['side']),'entry_bands':group(settled,lambda r:band(r['entry_price'],[0,.5,.6,.65,.7,.75,1.01])),'time_left_bands':group(settled,lambda r:band(r['time_left'],[0,60,120,180,240,301])),'score_bands':group(settled,lambda r:band(r['confidence'],[0,.65,.7,.75,.8,.85,.91,1.01])),'risk_revisions':group(settled,lambda r:(r['config_revision'],r['requested_cash'])),'order_statuses':dict(collections.Counter(o['status'] for o in orders.values())),'order_amounts':dict(collections.Counter(o['requested_cash'] for o in orders.values())),'simulation_event_counts':dict(event_counts),'top_event_messages':[{'code':k[0],'message':k[1],'count':v} for k,v in messages.most_common(12)],'execution_policy':policy,'checks':{'settled_pnl_reconciles':abs(sum(r['pnl'] for r in settled)-acct['realized_pnl'])<1e-5,'binary_payoffs_reconcile':all(abs(r['payout']-(r['shares'] if r['side']==r['outcome'] else 0))<1e-5 for r in settled),'per_trade_pnl_reconciles':all(abs(r['pnl']-(r['payout']-r['stake']))<1e-5 for r in settled),'all_official_settlements':all('POLYMARKET' in str(r['settlement_source']).upper() for r in settled)}}
 result['strategies'][key]=rec
 print(key,json.dumps({k:rec[k] for k in ['settled','open_risk','book_equity','max_realized_drawdown','max_loss_streak','checks']},ensure_ascii=False))
 print('SIDES',key,json.dumps(rec['sides'],ensure_ascii=False));print('REVISIONS',key,json.dumps(rec['risk_revisions'],ensure_ascii=False))
print('SNAPSHOT_WINDOW',stamp(result['snapshot']['snapshot_started_at']),stamp(result['snapshot']['snapshot_finished_at']))
(ROOT/'analysis.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));(ROOT/'trade-details.json').write_text(json.dumps(allrows,ensure_ascii=False,indent=2))
keys=['strategy','id','round_id','side','status','opened_cst','settled_cst','stake','shares','entry_price','fee','confidence','pnl','settlement_source','time_left','slippage','arrival_seconds','config_revision','requested_cash','reason']
with (ROOT/'trades.csv').open('w',newline='',encoding='utf-8-sig') as f:
 w=csv.DictWriter(f,fieldnames=keys,extrasaction='ignore');w.writeheader()
 for k,rows in allrows.items():w.writerows(dict(r,strategy=k) for r in rows)
