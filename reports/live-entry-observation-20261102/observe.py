"""Bounded read-only observation; no trading clients or mutation endpoints."""
import argparse
from collections import Counter
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[2]


def write_json(path,value):
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2))
    temporary.replace(path)


def observe(out,until,interval):
    out.mkdir(parents=True,exist_ok=False)
    start=time.time();previous=start;counts=Counter();healthy=0;samples=0;gaps=[];orders=[];errors=Counter()
    metadata={'started_at':start,'started_local':datetime.fromtimestamp(start).isoformat(),
              'planned_end_at':until,'planned_end_local':datetime.fromtimestamp(until).isoformat(),
              'pid':os.getpid(),'interval_seconds':interval,'read_only':True,'output':str(out),
              'scope':'MAIN status samples plus owned BUY orders and execution events',
              'limitations':['Sampling may miss short-lived signals.',
                             'Reason percentages are first-blocking-state samples, not missed-order rates.',
                             'This process does not place orders, change settings or send notifications.']}
    write_json(out/'metadata.json',metadata)
    conn=sqlite3.connect('file:'+str(ROOT/'data/live-accounts/accounts.sqlite3')+'?mode=ro',uri=True,timeout=2)
    conn.row_factory=sqlite3.Row
    cursor=conn.execute('SELECT coalesce(max(id),0) FROM events').fetchone()[0]
    try:
        with (out/'samples.jsonl').open('a',buffering=1) as log,(out/'events.jsonl').open('a',buffering=1) as events:
            while time.time()<until:
                began=time.time();row={'at':began,'local':datetime.fromtimestamp(began).isoformat(timespec='seconds')}
                if began-previous>interval*3:gaps.append({'from':previous,'to':began,'seconds':round(began-previous,1)})
                previous=began
                try:
                    with urllib.request.urlopen('http://127.0.0.1:8791/api/status',timeout=5) as response:data=json.load(response)
                    runtime=data['runtime'];live=runtime['managed_live'];main=next(v for v in live['variants'] if v['variant_id']=='MAIN')
                    market=live.get('market_data',{});round_data=market.get('market') or {};execution=main.get('execution') or {}
                    feed=market.get('feed_health') or {}
                    row.update(round_id=round_data.get('round_id'),time_left=round_data.get('ends_at',0)-began,
                               spot=market.get('spot'),target=round_data.get('target_price'),quotes=market.get('quotes'),
                               reference_ready=market.get('reference_ready'),reference_verified=market.get('reference_verified'),
                               execution=execution,signal=main.get('last_signal'),risk=main.get('risk'),feed=feed,
                               master_enabled=live.get('enabled'),strategy_settings=main.get('settings'),
                               wallet_ready=live.get('wallet',{}).get('ready'),error=live.get('error'),
                               entry_block=live.get('entry_block_reason'),continuity=runtime.get('continuity'))
                    reason=execution.get('reason') or '状态未知'
                    # Preserve the exact primary reason; do not infer how many
                    # trades would have occurred without that blocking condition.
                    counts[reason]+=1
                    healthy+=feed.get('fresh_sides')==2
                except Exception as exc:
                    row['observation_error']=type(exc).__name__;errors[type(exc).__name__]+=1;counts['观察接口暂不可用']+=1
                try:
                    orders=[dict(r) for r in conn.execute(
                        "SELECT id,created_at,round_id,state,requested_cash,confirmed_shares,confirmed_notional,confirmed_fee "
                        "FROM orders WHERE strategy_id='MAIN' AND action='BUY' AND created_at>=? ORDER BY created_at",(start,))]
                    batch=conn.execute('SELECT id,at,kind,message FROM events WHERE id>? ORDER BY id LIMIT 1000',(cursor,)).fetchall()
                    for event in batch:events.write(json.dumps(dict(event),ensure_ascii=False)+'\n')
                    if batch:cursor=batch[-1]['id']
                except Exception as exc:row['ledger_read_error']=type(exc).__name__;errors['ledger:'+type(exc).__name__]+=1
                log.write(json.dumps(row,ensure_ascii=False)+'\n');samples+=1
                summary={**metadata,'last_checked_at':time.time(),'samples':samples,'primary_reasons':dict(counts.most_common()),
                         'both_books_fresh_samples':healthy,'observer_errors':dict(errors),'observation_gaps':gaps,
                         'orders':orders,'buy_orders':len(orders),'orders_with_confirmed_fill':sum(o['confirmed_shares']>0 for o in orders),
                         'last_state':row,'finished':False}
                write_json(out/'summary.json',summary)
                time.sleep(max(0,min(interval-(time.time()-began),until-time.time())))
    finally:
        conn.close()
        path=out/'summary.json'
        if path.exists():
            summary=json.loads(path.read_text());summary.update(finished=True,ended_at=time.time());write_json(path,summary)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--out',required=True);parser.add_argument('--until',type=float,required=True)
    parser.add_argument('--interval',type=float,default=5)
    args=parser.parse_args()
    if not time.time()<args.until<=time.time()+6*3600:parser.error('Observation must end within six hours')
    if not 2<=args.interval<=60:parser.error('Interval must be between 2 and 60 seconds')
    observe(Path(args.out).resolve(),args.until,args.interval)
