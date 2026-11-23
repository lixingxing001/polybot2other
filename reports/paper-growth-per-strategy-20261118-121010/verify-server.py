from pathlib import Path
import hashlib,json,sqlite3,subprocess,time,urllib.request
app=Path('/opt/edgepulse/app');data=Path('/opt/edgepulse/data');backup=Path('/opt/edgepulse/backups/paper-growth-strategy-20261118-121010')
base=json.loads((backup/'manifest.json').read_text()); expected=json.loads((backup/'deployed-files.json').read_text())
c=urllib.request.build_opener(urllib.request.ProxyHandler({}))
def get(path):
 with c.open('http://127.0.0.1:8791'+path,timeout=20) as f:return json.load(f)
growth=get('/api/paper-growth');status=get('/api/status')['runtime'];managed=get('/api/managed-live')
checks={
 'new_files_match':all(hashlib.sha256((app/f).read_bytes()).hexdigest()==h for f,h in expected.items()),
 'other_sources_unchanged':all(hashlib.sha256((app/f).read_bytes()).hexdigest()==h for f,h in base['source_hashes'].items() if f not in expected),
 'settings_unchanged':hashlib.sha256(json.dumps(managed['settings'],sort_keys=True).encode()).hexdigest()==base['risk']['settings_sha256'],
 'live_runtime_disabled':managed['runtime_enabled'] is False,
 'five_independent_strategies':growth.get('complete') is True and growth.get('strategy_count')==5 and growth.get('scope')=='paper_by_strategy',
 'no_combined_status_or_profit':not any(k in growth for k in ['state','windows','today_curve']),
 'read_only_indicator':growth.get('policy',{}).get('controls_orders') is False,
 'fresh_growth_snapshot':0<=time.time()-growth['as_of']<30,
}
ledgers={};ledger_rows={};ledger_opens={}
for f,b in base['ledgers'].items():
 conn=sqlite3.connect((data/f).as_uri()+'?mode=ro',uri=True,timeout=3);conn.row_factory=sqlite3.Row;conn.execute('BEGIN')
 account=dict(conn.execute('SELECT * FROM account WHERE id=1').fetchone());rows=[dict(r) for r in conn.execute('SELECT * FROM trades ORDER BY id')]
 fingerprints={str(r['id']):hashlib.sha256(json.dumps(r,sort_keys=True,ensure_ascii=False).encode()).hexdigest() for r in rows if r['status']=='SETTLED'}
 ledgers[f]={'same_account':account['created_at']==b['created_at'] and account['initial_balance']==b['initial_balance'],'settled_history_preserved':all(fingerprints.get(k)==h for k,h in b['settled_rows_sha256'].items()),'trade_count_before':b['trade_count'],'trade_count_after':len(rows)}
 ledger_rows[f]=[r for r in rows if r['status']=='SETTLED'];ledger_opens[f]=[r for r in rows if r['status']=='OPEN' and r['opened_at']<growth['as_of']];conn.close()
checks['ledgers_preserved']=all(x['same_account'] and x['settled_history_preserved'] and x['trade_count_after']>=x['trade_count_before'] for x in ledgers.values())
window_checks={};strategy_summaries=[]
for s in growth.get('strategies',[]):
 f='polybot2other-real-btc.sqlite3' if s['strategy_id']=='MAIN' else 'strategy-experiments/'+s['strategy_id'].lower()+'.sqlite3'
 assert f in ledger_rows
 for key,w in s.get('windows',{}).items():
  rows=[r for r in ledger_rows[f] if w['start_at']<=r['settled_at']<w['end_at']]
  pnl_ok=abs(sum(r['pnl'] for r in rows)-w['pnl'])<.00001
  counts_ok=len(rows)==w['trades'] and len({r['round_id'] for r in rows})==w['markets'] and sum(r['pnl']>0 for r in rows)==w['wins']
  window_checks[s['strategy_id']+':'+key]=pnl_ok and counts_ok
 latest=max((r['settled_at'] for r in ledger_rows[f] if r['settled_at']<growth['as_of']),default=None)
 window_checks[s['strategy_id']+':latest_settlement']=s['latest_settlement_at']==latest
 opened=ledger_opens[f];window_checks[s['strategy_id']+':open_positions']=s['open_positions']==len(opened) and abs(s['open_cost']-sum(r['stake'] for r in opened))<.00001
 strategy_summaries.append({'name':s['name'],'state':s['state']['label'],'today':s['windows']['today']['pnl'],'last1h':s['windows']['last1h']['pnl'],'last3h':s['windows']['last3h']['pnl']})
checks['each_strategy_reconciles_with_own_ledger']=bool(window_checks) and all(window_checks.values())
services={name:subprocess.run(['systemctl','is-active',name],capture_output=True,text=True).stdout.strip() for name in base['services']}
checks['existing_services_unchanged']=services==base['services']
container=json.loads(subprocess.check_output(['docker','inspect','edgepulse-paper']))[0]
checks['container_healthy']=container['State'].get('Health',{}).get('Status')=='healthy';checks['runtime_no_error']=status.get('last_error') is None
market=status.get('managed_live',{}).get('market_data',{})
result={'at':time.time(),'checks':checks,'ledgers':ledgers,'window_checks':window_checks,'growth':growth,'strategies':strategy_summaries,'services':services,'container':{'image':container['Image'],'started_at':container['State']['StartedAt'],'health':container['State'].get('Health',{}).get('Status'),'restart_count':container['RestartCount']},'reference':{k:market.get(k) for k in ['reference_ready','reference_verified','observed_at','error']},'settings_revision':managed['settings']['revision']}
(backup/'verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False))
