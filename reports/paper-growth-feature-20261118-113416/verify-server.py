from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import hashlib,json,sqlite3,subprocess,time,urllib.request
app=Path('/opt/edgepulse/app');data=Path('/opt/edgepulse/data');backup=Path('/opt/edgepulse/backups/paper-growth-20261118-113416')
base=json.loads((backup/'manifest.json').read_text()); expected=json.loads((backup/'deployed-files.json').read_text())
c=urllib.request.build_opener(urllib.request.ProxyHandler({}))
def get(path):
 with c.open('http://127.0.0.1:8791'+path,timeout=20) as f:return json.load(f)
growth=get('/api/paper-growth');status=get('/api/status')['runtime'];managed=get('/api/managed-live')
checks={}
checks['new_files_match']=all(hashlib.sha256((app/f).read_bytes()).hexdigest()==h for f,h in expected.items())
checks['other_sources_unchanged']=all(hashlib.sha256((app/f).read_bytes()).hexdigest()==h for f,h in base['source_hashes'].items() if f not in expected)
checks['settings_unchanged']=hashlib.sha256(json.dumps(managed['settings'],sort_keys=True).encode()).hexdigest()==base['risk']['settings_sha256']
checks['live_runtime_disabled']=managed['runtime_enabled'] is False
checks['growth_available']=growth.get('available') is True and growth.get('account_count')==5
checks['read_only_indicator']=growth.get('policy',{}).get('controls_orders') is False
checks['fresh_growth_snapshot']=0<=time.time()-growth['as_of']<30
ledgers={};all_rows=[]
for f,b in base['ledgers'].items():
 conn=sqlite3.connect((data/f).as_uri()+'?mode=ro',uri=True,timeout=3);conn.row_factory=sqlite3.Row
 conn.execute('BEGIN');account=dict(conn.execute('SELECT * FROM account WHERE id=1').fetchone())
 rows=[dict(r) for r in conn.execute('SELECT * FROM trades ORDER BY id')]
 fingerprints={str(r['id']):hashlib.sha256(json.dumps(r,sort_keys=True,ensure_ascii=False).encode()).hexdigest() for r in rows if r['status']=='SETTLED'}
 ledgers[f]={'same_account':account['created_at']==b['created_at'] and account['initial_balance']==b['initial_balance'],'settled_history_preserved':all(fingerprints.get(k)==h for k,h in b['settled_rows_sha256'].items()),'trade_count_before':b['trade_count'],'trade_count_after':len(rows)}
 all_rows.extend(r for r in rows if r['status']=='SETTLED');conn.close()
checks['ledgers_preserved']=all(x['same_account'] and x['settled_history_preserved'] and x['trade_count_after']>=x['trade_count_before'] for x in ledgers.values())
window_checks={}
for key,w in growth.get('windows',{}).items():
 rows=[r for r in all_rows if w['start_at']<=r['settled_at']<w['end_at']]
 correct_pnl=abs(sum(r['pnl'] for r in rows)-w['pnl'])<.00001
 correct_counts=len(rows)==w['trades'] and len({r['round_id'] for r in rows})==w['markets']
 window_checks[key]=correct_pnl and correct_counts
checks['windows_reconcile_with_ledgers']=bool(window_checks) and all(window_checks.values())
services={name:subprocess.run(['systemctl','is-active',name],capture_output=True,text=True).stdout.strip() for name in base['services']}
checks['existing_services_unchanged']=services==base['services']
container=json.loads(subprocess.check_output(['docker','inspect','edgepulse-paper']))[0]
checks['container_healthy']=container['State'].get('Health',{}).get('Status')=='healthy'
checks['runtime_no_error']=status.get('last_error') is None
market=status.get('managed_live',{}).get('market_data',{})
readiness={k:market.get(k) for k in ['reference_ready','reference_verified','observed_at','error']}
result={'at':time.time(),'checks':checks,'ledgers':ledgers,'window_checks':window_checks,'growth':growth,'services':services,'container':{'image':container['Image'],'started_at':container['State']['StartedAt'],'health':container['State'].get('Health',{}).get('Status'),'restart_count':container['RestartCount']},'reference':readiness,'settings_revision':managed['settings']['revision']}
(backup/'verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
print(json.dumps(result,ensure_ascii=False))
