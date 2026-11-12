"""Archive finished retired simulations and remove their code/data after stop."""
from pathlib import Path
import hashlib
import json
import shutil
import sqlite3
import subprocess
import time
import zipfile

ROOT=Path(__file__).resolve().parents[2];REPORT=Path(__file__).resolve().parent
DATA=['data/hengzhun-v1-pilot','data/paired-paper-v1','data/paired-paper-v2','data/paired-paper-v1-development-20261107-040432']
ARCHIVE=ROOT/'data/archives/retired-strategies-20261111-224000'
KEEP=['pair_wallet','pair_settings','pair_groups','pair_orders','pair_fills','pair_profit','pair_curve',
      'pair_v2_identity','pair_v2_halt','pilot_state']

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()

def main():
    opened=subprocess.run(['lsof','-n','-P','-Fn']+['+D',str(ROOT/DATA[0])],capture_output=True,text=True)
    assert not opened.stdout.strip(),'Retired process still using Paper data'
    ARCHIVE.mkdir(parents=True,exist_ok=False);staging=ARCHIVE/'compact';staging.mkdir()
    summaries={};source_stats={}
    for folder in DATA:
        root=ROOT/folder;assert root.is_dir() and root.resolve()==root
        check=subprocess.run(['lsof','-n','-P','-Fn','+D',str(root)],capture_output=True,text=True)
        assert check.returncode in (0,1) and not check.stdout.strip(),folder+' remains open'
        files=[p for p in root.rglob('*') if p.is_file()]
        assert all(not p.is_symlink() and p.stat().st_dev==ROOT.stat().st_dev for p in root.rglob('*'))
        source_stats[folder]={'allocated_bytes':sum(p.stat().st_blocks*512 for p in files),'files':len(files)}
        for source in root.glob('*.sqlite3'):
            db=sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row;db.execute('BEGIN')
            existing={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'pair_wallet' not in existing:db.close();continue
            active=db.execute('SELECT count(*) FROM pair_groups WHERE closed=0').fetchone()[0]
            if 'development' not in folder:
                assert active==0,(folder,active)
                assert db.execute("SELECT count(*) FROM pair_orders WHERE status IN ('PENDING','OPEN','CANCELING','DRAINING')").fetchone()[0]==0
                assert db.execute("SELECT count(*) FROM pair_fills WHERE status='MATCHED'").fetchone()[0]==0
            dest=staging/root.name/source.name;dest.parent.mkdir(parents=True,exist_ok=True)
            output=sqlite3.connect(dest);counts={}
            with output:
                for table in KEEP:
                    if table not in existing:continue
                    output.execute(db.execute('SELECT sql FROM sqlite_master WHERE name=?',(table,)).fetchone()[0])
                    rows=db.execute('SELECT * FROM '+table).fetchall();counts[table]=len(rows)
                    if rows:output.executemany('INSERT INTO '+table+' VALUES('+','.join('?' for _ in rows[0])+')',rows)
            assert output.execute('PRAGMA integrity_check').fetchone()[0]=='ok';output.close()
            summaries[str(source.relative_to(ROOT))]={'wallet':dict(db.execute('SELECT * FROM pair_wallet').fetchone()),'active_groups':active,'preserved_tables':counts}
            db.close()
        # The small immutable seed remains reproducible in the archive.
        if (root/'seed').exists():shutil.copytree(root/'seed',staging/root.name/'seed')
    archive=ARCHIVE/'financial-history.zip'
    with zipfile.ZipFile(archive,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for p in staging.rglob('*'):
            if p.is_file():z.write(p,str(p.relative_to(staging)))
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        for p in staging.rglob('*'):
            if p.is_file():assert hashlib.sha256(z.read(str(p.relative_to(staging)))).hexdigest()==sha(p)
    manifest={'at_ms':int(time.time()*1000),'strategy_scope':'Retired Hengzhun and Paired strategies only',
              'preserved':summaries,'removed_working_data':source_stats,'archive_sha256':sha(archive),
              'not_preserved':'Repeated raw quote/event/price working tables; compact financial history and seed are archived.'}
    (ARCHIVE/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    shutil.rmtree(staging)
    source=json.loads((REPORT/'source-retirement.json').read_text())
    with zipfile.ZipFile(REPORT/source['archive']) as z:
        for relative in source['remove_files']:
            p=ROOT/relative
            assert sha(p)==source['hashes'][relative] and hashlib.sha256(z.read(relative)).hexdigest()==sha(p)
    for relative in source['remove_files']:(ROOT/relative).unlink()
    package=ROOT/'src/polybot2other/hengzhun'
    for p in package.rglob('*'):
        assert p.is_dir() or '__pycache__' in p.parts,p
    shutil.rmtree(package)
    for pattern in ['paired_paper*.pyc','paired_v2*.pyc']:
        for p in (ROOT/'src/polybot2other/__pycache__').glob(pattern):p.unlink()
    for relative in DATA:
        root=ROOT/relative
        check=subprocess.run(['lsof','-n','-P','-Fn','+D',str(root)],capture_output=True,text=True)
        assert not check.stdout.strip()
        shutil.rmtree(root)
    (REPORT/'retired-financial-summary.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    print(json.dumps({'retired_data':DATA,'source_files_removed':len(source['remove_files']),
                      'history_archive':str(archive),'archive_MB':round(archive.stat().st_size/1e6,2),
                      'wallets':{p:v['wallet'] for p,v in summaries.items()}},ensure_ascii=False),flush=True)

if __name__=='__main__':main()
