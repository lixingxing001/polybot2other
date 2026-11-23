from pathlib import Path
import subprocess,json
app=Path('/opt/edgepulse/app');backup=Path('/opt/edgepulse/backups/paper-growth-strategy-20261118-121010');base=json.loads((backup/'manifest.json').read_text())
current=json.loads(subprocess.check_output(['docker','inspect','edgepulse-paper']))[0]['Image'];assert current==base['image'],'running image changed'
r=subprocess.run(['docker','compose','up','-d','--no-build','--wait','--wait-timeout','180','dashboard'],cwd=app,text=True,capture_output=True,timeout=240)
(backup/'release.log').write_text(r.stdout+r.stderr);print(r.stdout+r.stderr)
if r.returncode:
 subprocess.run(['docker','tag',base['rollback_image'],'edgepulse:paper'],check=True)
 recovery=subprocess.run(['docker','compose','up','-d','--no-build','--wait','--wait-timeout','180','dashboard'],cwd=app,text=True,capture_output=True,timeout=240)
 print('rolled back to previous image',recovery.returncode);print(recovery.stdout+recovery.stderr)
raise SystemExit(r.returncode)
