from pathlib import Path
import hashlib,json,sqlite3,subprocess,time,urllib.request,shutil,os
os.umask(0o077)
app=Path('/opt/edgepulse/app'); data=Path('/opt/edgepulse/data')
backup=Path('/opt/edgepulse/backups/paper-growth-strategy-20261118-121010'); backup.mkdir(parents=True,exist_ok=False)
files=['src/polybot2other/paper_growth.py', 'src/polybot2other/static/index.html', 'src/polybot2other/static/app.js', 'src/polybot2other/static/paper-growth.js', 'src/polybot2other/static/paper-growth.css']
for f in files:
 target=backup/'source'/f;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(app/f,target)
image=json.loads(subprocess.check_output(['docker','inspect','edgepulse-paper']))[0]['Image']
assert image=='sha256:10a3011740d3e08873ad68bfe8186ca76a584057802d7b00cd8964bbae557717', 'image changed'
tag='edgepulse:paper-before-growth-strategy-20261118-121010'
subprocess.run(['docker','tag',image,tag],check=True)
ledger_names=['polybot2other-real-btc.sqlite3','strategy-experiments/main_optimized.sqlite3','strategy-experiments/main_adaptive.sqlite3','strategy-experiments/main_optimized_fast.sqlite3','strategy-experiments/main_optimized_plus.sqlite3']
ledgers={}; db_count=0
for source in data.rglob('*.sqlite3'):
 rel=source.relative_to(data);target=backup/'data'/rel;target.parent.mkdir(parents=True,exist_ok=True)
 src=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True,timeout=5); dst=sqlite3.connect(target)
 try:src.backup(dst,pages=512,sleep=.02)
 finally:src.close();dst.close()
 db_count+=1
 if str(rel) in ledger_names:
  c=sqlite3.connect(target);c.row_factory=sqlite3.Row
  a=dict(c.execute('SELECT * FROM account WHERE id=1').fetchone())
  rows=[dict(r) for r in c.execute("SELECT * FROM trades WHERE status='SETTLED' ORDER BY id")]
  fingerprints={str(r['id']):hashlib.sha256(json.dumps(r,sort_keys=True,ensure_ascii=False).encode()).hexdigest() for r in rows}
  ledgers[str(rel)]={'created_at':a['created_at'],'initial_balance':a['initial_balance'],'trade_count':c.execute('SELECT count(*) FROM trades').fetchone()[0],'settled_count':len(rows),'settled_rows_sha256':fingerprints}
  c.close()
c=urllib.request.build_opener(urllib.request.ProxyHandler({}))
with c.open('http://127.0.0.1:8791/api/managed-live',timeout=20) as f:d=json.load(f)
settings=d['settings']
risk={'revision':settings['revision'],'enabled':settings['enabled'],'runtime_enabled':d['runtime_enabled'],'strategies':{k:{field:v.get(field) for field in ['enabled','stake_dollars','max_open_trades','max_daily_loss']} for k,v in settings['strategies'].items()},'settings_sha256':hashlib.sha256(json.dumps(settings,sort_keys=True).encode()).hexdigest()}
services={name:subprocess.run(['systemctl','is-active',name],capture_output=True,text=True).stdout.strip() for name in ['nginx','mysqld','php-fpm-82','ssh']}
manifest={'at':time.time(),'backup_directory':str(backup),'rollback_image':tag,'image':image,'databases_backed_up':db_count,'ledgers':ledgers,'risk':risk,'services':services,'source_hashes':{str(p.relative_to(app)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (app/'src').rglob('*') if p.is_file() and '__pycache__' not in p.parts}}
(backup/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
print(json.dumps(manifest,ensure_ascii=False))
