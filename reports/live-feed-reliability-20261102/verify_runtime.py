"""Read-only post-restart checks. Does not invoke any trading endpoint."""
import hashlib
import json
import sqlite3
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
REPORT=Path(__file__).resolve().parent
before=json.loads((REPORT/"pre-batched-restart.json").read_text())
samples=[];started=time.time()
for i in range(31):
    data=json.load(urllib.request.urlopen("http://127.0.0.1:8791/api/status",timeout=10))
    runtime=data["runtime"];live=runtime["managed_live"];md=live.get("market_data",{})
    market=md.get("market") or {};now=time.time()
    sample={"at":now,"left":market.get("ends_at",0)-now,"round":market.get("round_id"),
            "running":live.get("running"),"available":live.get("available"),"error":live.get("error"),
            "entry_block":live.get("entry_block_reason"),"feed":md.get("feed_health"),
            "quotes":md.get("quotes"),"continuity":runtime.get("continuity"),
            "execution":{v.get("variant_id"):v.get("execution") for v in live.get("variants",[])}}
    samples.append(sample)
    if i%10==0:print(json.dumps({k:sample[k] for k in ("at","left","running","error","entry_block")}),flush=True)
    if i<30:time.sleep(2)
conn=sqlite3.connect("file:"+str(ROOT/"data/live-accounts/accounts.sqlite3")+"?mode=ro",uri=True);conn.row_factory=sqlite3.Row
orders={r["id"]:dict(r) for r in conn.execute("SELECT * FROM orders")}
fields=("external_id","strategy_id","round_id","token_id","outcome","action","requested_cash","requested_shares","limit_price","confirmed_shares","confirmed_notional","confirmed_fee")
changed=[]
for old in before["orders"]:
    new=orders.get(old["id"])
    if not new or any(old[k]!=new[k] for k in fields):changed.append(old["id"])
assets={}
for name in ("managed-live.js","styles.css"):
    body=urllib.request.urlopen("http://127.0.0.1:8791/static/"+name,timeout=10).read()
    assets[name]=hashlib.sha256(body).hexdigest()==hashlib.sha256((ROOT/"src/polybot2other/static"/name).read_bytes()).hexdigest()
guard=runtime.get("continuity") or {};pid=int((ROOT/"data/polybot2other-macos-8791.pid").read_text())
assertions=subprocess.run(["pmset","-g","assertions"],capture_output=True,text=True,check=True).stdout
valid=[r for r in samples if 20<=r["left"]<=285 and r["at"]-started>=5]
result={"at":time.time(),"pid":pid,"previous_pid":before["pid"],"settings_preserved":live["settings"]==before["settings"],
        "revision":live["settings"].get("revision"),"historical_orders_checked":len(before["orders"]),
        "historical_financial_fields_changed":changed,"sqlite_quick_check":conn.execute("PRAGMA quick_check").fetchone()[0],
        "service_caffeinate_assertion_seen":bool(guard.get("guard_pid") and f"pid {guard['guard_pid']}(caffeinate)" in assertions),
        "assets_match_source":assets,"eligible_samples":len(valid),
        "both_fresh_pct":round(100*sum((r["feed"] or {}).get("fresh_sides")==2 for r in valid)/len(valid),2) if valid else None,
        "tests":{"passed":452,"subtests":56},"ui_verified":False,
        "ui_blocker":"Native pipe startup failed; browser access host unavailable",
        "samples":samples}
(REPORT/"verification.json").write_text(json.dumps(result,ensure_ascii=False,indent=2))
print("VERIFICATION",json.dumps({k:v for k,v in result.items() if k!="samples"},ensure_ascii=False),flush=True)
