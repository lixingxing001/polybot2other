"""Prepare a narrowly scoped, user-requested cleanup; does not delete anything."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[2]
REPORT=Path(__file__).resolve().parent
ARCHIVE=ROOT/'data/archives/retired-research-20261111-223500'
CAPTURES=['data/hengzhun-v1','data/hengzhun-v1-startup-overflow-20261107-015032']
DIAGNOSTIC_PARENTS=['data/backups/before-macos-start-20261026-222638/strategy-experiments',
                    'data/backups/strategy-retirement-20261026-225605/databases/strategy-experiments']
CANONICAL=ROOT/'data/backups/strategy-retirement-20261026-225605/retired-runtime/strategy-experiments'
TABLES=['capture_meta','market_contracts','price_observations','landmarks','official_results',
        'proxy_samples','condition_failures','result_poll_state']

def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(4*1024*1024),b''):h.update(chunk)
    return h.hexdigest()

def ro(path):return sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=10)

def status():
    with urllib.request.urlopen('http://127.0.0.1:8791/api/status',timeout=20) as response:return json.load(response)

def stat(path):
    s=path.stat()
    return {'path':str(path.relative_to(ROOT)),'size':s.st_size,'allocated':s.st_blocks*512,
            'inode':s.st_ino,'device':s.st_dev,'mtime_ns':s.st_mtime_ns}

def main():
    ARCHIVE.mkdir(parents=True,exist_ok=False)
    initial=status();runtime=initial['runtime'];assert runtime['hengzhun']['pilot'] is True
    protected={'pid':(ROOT/'data/polybot2other-macos-8791.pid').read_text().strip(),
               'live_settings':runtime['managed_live']['settings'],
               'financial_tables':{},'pilot_seed_files':{}}
    stores={'data/polybot2other-real-btc.sqlite3':['account','trades','orders'],
            'data/strategy-experiments/main_optimized.sqlite3':['account','trades','orders'],
            'data/strategy-experiments/main_tp_sl.sqlite3':['account','trades','orders'],
            'data/strategy-experiments/value_confirm_v1.sqlite3':['account','trades','orders'],
            'data/live-accounts/accounts.sqlite3':['positions','orders','fills'],
            'data/hengzhun-v1-pilot/paper.sqlite3':['pair_wallet','pair_groups','pair_orders','pair_fills'],
            'data/paired-paper-v1/paper.sqlite3':['pair_wallet','pair_groups','pair_orders','pair_fills'],
            'data/paired-paper-v2/paper.sqlite3':['pair_wallet','pair_groups','pair_orders','pair_fills'],
            'data/paired-paper-v2/stress.sqlite3':['pair_wallet','pair_groups','pair_orders','pair_fills']}
    for file,tables in stores.items():
        with ro(ROOT/file) as db:
            db.execute('BEGIN');present={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            protected['financial_tables'][file]={}
            for table in tables:
                if table not in present:continue
                rows=db.execute('SELECT * FROM '+table+' ORDER BY rowid').fetchall()
                protected['financial_tables'][file][table]={'count':len(rows),'sha256':hashlib.sha256(json.dumps(rows,sort_keys=True,ensure_ascii=False).encode()).hexdigest()}
    for p in (ROOT/'data/hengzhun-v1-pilot/seed').iterdir():
        if p.is_file():protected['pilot_seed_files'][str(p.relative_to(ROOT))]=sha(p)
    (REPORT/'protected-before.json').write_text(json.dumps(protected,ensure_ascii=False,indent=2))
    inventory=[];exports=[]
    for relative in CAPTURES:
        source=ROOT/relative;dest=ARCHIVE/source.name;dest.mkdir()
        assert source.resolve()==source and source.is_dir()
        for p in source.rglob('*'):
            assert not p.is_symlink() and p.stat().st_dev==ROOT.stat().st_dev
        db=ro(source/'events.sqlite3');db.execute('BEGIN')
        out=sqlite3.connect(dest/'research-summary.sqlite3')
        counts={};present={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        with out:
            for table in TABLES:
                if table not in present:continue
                schema=db.execute('SELECT sql FROM sqlite_master WHERE type=? AND name=?',('table',table)).fetchone()[0]
                out.execute(schema);cur=db.execute('SELECT * FROM '+table);n=0
                while True:
                    batch=cur.fetchmany(10000)
                    if not batch:break
                    out.executemany('INSERT INTO '+table+' VALUES('+','.join('?' for _ in batch[0])+')',batch);n+=len(batch)
                counts[table]=n
            out.execute('CREATE TABLE cleanup_evidence(json TEXT NOT NULL)')
            summary={'source':relative,'raw_events_preserved':False,'feature_payloads_preserved':False,
                     'full_event_replay_available':False,'formal_training_qualification':'NOT_ESTABLISHED',
                     'raw_last_ingest_seq':db.execute('SELECT max(seq) FROM raw_events').fetchone()[0],
                     'preserved_tables':counts,'quality_summary':[list(r) for r in db.execute('SELECT code,count(*),min(at),max(at) FROM quality_events GROUP BY code')],
                     'reason':'User requested unused data outside the four active strategies be cleaned; inactive full raw research capture is no longer a runtime dependency.'}
            out.execute('INSERT INTO cleanup_evidence VALUES(?)',(json.dumps(summary,ensure_ascii=False),))
        assert out.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        for table,n in counts.items():assert out.execute('SELECT count(*) FROM '+table).fetchone()[0]==n
        out.close();db.close()
        if (source/'ledger.sqlite3').exists():
            with ro(source/'ledger.sqlite3') as src,sqlite3.connect(dest/'ledger.sqlite3') as dst:src.backup(dst)
        for p in source.rglob('*.json'):
            target=dest/'metadata'/p.relative_to(source);target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,target)
        summary['compact_sha256']=sha(dest/'research-summary.sqlite3')
        (dest/'cleanup-manifest.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2));exports.append(summary)
        for p in source.rglob('*'):
            if p.is_file():inventory.append({**stat(p),'category':'inactive_research_capture'})
    # Retain the final retired-runtime copy of each diagnostic experiment;
    # remove only its earlier backup snapshots, never live or named four-strategy stores.
    for relative in DIAGNOSTIC_PARENTS:
        for p in (ROOT/relative).iterdir():
            if not p.name.startswith('single_fak') or not p.name.endswith(('_diagnostic.sqlite3','_diagnostic.sqlite3-wal','_diagnostic.sqlite3-shm')):continue
            base=p.name.split('.sqlite3')[0]+'.sqlite3'
            assert (CANONICAL/base).is_file()
            assert not p.is_symlink() and p.stat().st_dev==ROOT.stat().st_dev
            inventory.append({**stat(p),'category':'superseded_diagnostic_backup','retained_final_copy':str((CANONICAL/base).relative_to(ROOT))})
    all_files=[x['path'] for x in inventory]
    tracked=subprocess.run(['git','ls-files','--']+all_files,cwd=ROOT,text=True,capture_output=True,check=True).stdout
    assert not tracked.strip(),'A planned deletion is tracked by Git'
    plan={'root':str(ROOT),'at_ms':int(time.time()*1000),'directories_to_remove':CAPTURES,'files':inventory,
          'allocated_bytes_to_remove':sum(x['allocated'] for x in inventory),'preserved_archive':str(ARCHIVE.relative_to(ROOT)),
          'preserved_exports':exports,'free_bytes_before':shutil.disk_usage(ROOT).free,
          'data_du_kib_before':int(subprocess.run(['du','-sk',str(ROOT/'data')],capture_output=True,text=True,check=True).stdout.split()[0])}
    (REPORT/'cleanup-plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2))
    print(json.dumps({'files':len(inventory),'allocated_GB_to_remove':round(plan['allocated_bytes_to_remove']/1e9,3),
                      'archive':str(ARCHIVE),'preserved_counts':[x['preserved_tables'] for x in exports]},ensure_ascii=False),flush=True)

if __name__=='__main__':main()
