"""Execute only the prepared allowlist, with fresh dependency and identity checks."""
from pathlib import Path
import hashlib
import json
import shutil
import sqlite3
import subprocess
import time

from prepare_cleanup import ROOT,REPORT,CAPTURES,DIAGNOSTIC_PARENTS,CANONICAL,status,sha,stat,ro

def main():
    plan=json.loads((REPORT/'cleanup-plan.json').read_text())
    before=json.loads((REPORT/'protected-before.json').read_text())
    runtime=status()['runtime']
    assert runtime['hengzhun']['pilot'] is True and runtime['hengzhun']['running'] is True
    assert (ROOT/'data/polybot2other-macos-8791.pid').read_text().strip()==before['pid']
    targets=[ROOT/row['path'] for row in plan['files']]
    assert len(targets)==len(set(targets))
    expected={row['path']:row for row in plan['files']}
    for p in targets:
        assert p.is_file() and not p.is_symlink() and p.resolve()==p
        item=stat(p);prior=expected[item['path']]
        for field in ['size','inode','device','mtime_ns']:
            assert item[field]==prior[field],f'Changed file: {p} {field}'
        if prior['category']=='inactive_research_capture':
            assert any(p.is_relative_to(ROOT/c) for c in CAPTURES)
        elif prior['category']=='superseded_diagnostic_backup':
            assert str(p.parent.relative_to(ROOT)) in DIAGNOSTIC_PARENTS
            assert p.name.startswith('single_fak') and p.name.endswith(('_diagnostic.sqlite3','_diagnostic.sqlite3-wal','_diagnostic.sqlite3-shm'))
            assert (ROOT/prior['retained_final_copy']).is_file()
        else:raise ValueError('Unexpected deletion category')
    for folder in CAPTURES:
        assert {str(p.relative_to(ROOT)) for p in (ROOT/folder).rglob('*') if p.is_file()} <= set(expected)
    opened=subprocess.run(['lsof','-n','-P','-Fn']+[str(p) for p in targets],capture_output=True,text=True)
    (REPORT/'last-open-file-check.txt').write_text(opened.stdout+opened.stderr)
    assert opened.returncode in (0,1) and not opened.stdout.strip(),'A target file is still open'
    for relative,digest in before['pilot_seed_files'].items():assert sha(ROOT/relative)==digest
    for item in plan['preserved_exports']:
        compact=ROOT/plan['preserved_archive']/Path(item['source']).name/'research-summary.sqlite3'
        assert sha(compact)==item['compact_sha256']
        with ro(compact) as c:assert c.execute('PRAGMA quick_check').fetchone()[0]=='ok'
    started=int(time.time()*1000)
    # Exact files only: never recursively delete an uninspected tree.
    with (REPORT/'deleted-files.jsonl').open('x') as journal:
        for p in targets:
            item=expected[str(p.relative_to(ROOT))]
            p.unlink()
            journal.write(json.dumps({'at_ms':int(time.time()*1000),**item},ensure_ascii=False)+'\n');journal.flush()
    for relative in CAPTURES:
        directory=ROOT/relative
        for p in sorted(directory.rglob('*'),key=lambda p:len(p.parts),reverse=True):
            assert p.is_dir() and not p.is_symlink();p.rmdir()
        directory.rmdir()
    result={'started_at_ms':started,'completed_at_ms':int(time.time()*1000),'deleted_files':len(targets),
            'removed_allocated_bytes':plan['allocated_bytes_to_remove'],
            'free_bytes_before':plan['free_bytes_before'],'free_bytes_after':shutil.disk_usage(ROOT).free,
            'data_du_kib_before':plan['data_du_kib_before'],
            'data_du_kib_after':int(subprocess.run(['du','-sk',str(ROOT/'data')],capture_output=True,text=True,check=True).stdout.split()[0])}
    (REPORT/'execution-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
