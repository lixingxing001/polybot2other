from pathlib import Path
import datetime,hashlib,json,os,shutil,socket,sqlite3
from polybot2other.storage import TradeStore
from polybot2other.optimized_main import OptimizedTradeStore
root=Path.cwd()
p=json.loads((root/'reports/realistic-paper-current.json').read_text());backup=Path(p['backup'])/'ledgers';report=Path(p['report'])
try:
    with socket.create_connection(('127.0.0.1',8791),timeout=.5):raise RuntimeError('Stop the dashboard before reset')
except (ConnectionRefusedError,TimeoutError,OSError):pass
backup.mkdir(parents=True,exist_ok=False);backup.chmod(0o700)
manifest={'at':datetime.datetime.now().astimezone().isoformat(),'initial_balance':100,'accounts':{},'archived_files':[]}
live=root/'data/live-accounts/accounts.sqlite3'
with sqlite3.connect(live.resolve().as_uri()+'?mode=ro',uri=True) as conn:
    config=json.loads(conn.execute('select json from live_config').fetchone()[0]);assert not config['enabled']
    manifest['live_config']=config
    with sqlite3.connect(backup/'live-before.sqlite3') as target:conn.backup(target)
    manifest['live_counts']={t:conn.execute('select count(*) from '+t).fetchone()[0] for t in ('orders','fills','positions')}
os.chmod(backup/'live-before.sqlite3',0o600)
paths={'MAIN':root/'data/polybot2other-real-btc.sqlite3','MAIN_OPTIMIZED':root/'data/strategy-experiments/main_optimized.sqlite3','MAIN_TP_SL':root/'data/strategy-experiments/main_tp_sl.sqlite3','VALUE_CONFIRM_V1':root/'data/strategy-experiments/value_confirm_v1.sqlite3'}
for key,path in paths.items():
    rel=path.relative_to(root);snapshot=backup/'snapshots'/rel;snapshot.parent.mkdir(parents=True,exist_ok=True)
    conn=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True);conn.row_factory=sqlite3.Row
    with sqlite3.connect(snapshot) as target:conn.backup(target)
    manifest['accounts'][key]={'before':dict(conn.execute('select * from account').fetchone()),'trades':conn.execute('select count(*) from trades').fetchone()[0],'snapshot':str(snapshot)}
    conn.close();snapshot.chmod(0o600)
for path in list(paths.values())+[root/'data/strategy-experiments'/s for s in ('main-comparison.json','main_optimized.decisions.jsonl','main_tp_sl.observations.jsonl','value_confirm_v1.decisions.jsonl')]:
    for source in ([path,Path(str(path)+'-wal'),Path(str(path)+'-shm')] if path.suffix=='.sqlite3' else [path]):
        if source.exists():
            target=backup/'originals'/source.relative_to(root);target.parent.mkdir(parents=True,exist_ok=True);shutil.move(source,target);manifest['archived_files'].append(str(target))
for key,path in paths.items():
    cls=OptimizedTradeStore if key=='MAIN_OPTIMIZED' else TradeStore
    store=cls(path,100);store.record_equity();account=store.account()
    assert account['initial_balance']==account['cash_balance']==100 and account['realized_pnl']==0
    assert store.open_trade_count()==0 and store.paper_order_count()==0
    manifest['accounts'][key]['after']=account;store.conn.close()
(report/'reset.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2));(backup/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
print(json.dumps({'reset_accounts':list(paths),'initial_balance':100,'backup':str(backup),'live_master_enabled':config['enabled'],'live_counts':manifest['live_counts']},ensure_ascii=False))
