#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"
PYTHON_BIN=".venv/macos/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  printf '%s\n' 'Mac Python environment missing. Run:' 'uv venv --python 3.12 .venv/macos' "uv pip install --python .venv/macos/bin/python -e . 'py_clob_client_v2==1.0.1'" >&2
  exit 1
fi

exec "$PYTHON_BIN" - <<'PY'
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

root = Path.cwd()
host, port = "127.0.0.1", 8791
url = f"http://{host}:{port}"
try:
    with socket.create_connection((host, port), timeout=1):
        raise SystemExit(f"Port {port} is already in use. Open {url} or inspect the existing process first.")
except OSError:
    pass

log_path = root / "logs" / "polybot2other-macos-8791.log"
log_path.parent.mkdir(exist_ok=True)
env = dict(os.environ)
env["PYTHONPATH"] = str(root / "src")
env["PYTHONUNBUFFERED"] = "1"
# Enable the new live feature; its master/per-strategy switches default OFF.
# Legacy live runners remain disabled when MANAGED_LIVE_ENABLED is true.
env.setdefault("POLYBOT2OTHER_LIVE_TRADING_RUNTIME_ENABLED", "true")
env["POLYBOT2OTHER_MANAGED_LIVE_ENABLED"] = "true"
env["POLYBOT2OTHER_PAPER_REALISTIC_EXECUTION"] = "true"
env.setdefault("POLYBOT2OTHER_HENGZHUN_ENABLED", "true")
env.setdefault("POLYBOT2OTHER_HENGZHUN_PILOT_ENABLED", "true")
env.setdefault("POLYBOT2OTHER_PAIRED_PAPER_ENABLED", "true")
env.setdefault("POLYBOT2OTHER_PAIRED_V1_FROZEN", "true")
env.setdefault("POLYBOT2OTHER_PAIRED_V2_ENABLED", "true")
env.setdefault("POLYBOT2OTHER_KEEP_AWAKE", "true")
env["POLYBOT2OTHER_STRATEGY_EXPERIMENTS_ENABLED"] = "true"
env["POLYBOT2OTHER_STRATEGY_EXPERIMENTS_VARIANTS"] = "MAIN_TP_SL,MAIN_OPTIMIZED,VALUE_CONFIRM_V1"
env["POLYBOT2OTHER_MARKET_SCOUT_ENABLED"] = "false"
env["POLYBOT2OTHER_LLM_SUPER_AGENT_ENABLED"] = "false"
with log_path.open("ab") as log:
    process = subprocess.Popen(
        [sys.executable, "-m", "polybot2other.web", "--host", host, "--port", str(port)],
        cwd=root,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
pid_path = root / "data" / "polybot2other-macos-8791.pid"
pid_path.write_text(f"{process.pid}\n")
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
deadline = time.monotonic() + 45
while time.monotonic() < deadline:
    if process.poll() is not None:
        raise SystemExit(f"Dashboard exited ({process.returncode}); inspect {log_path}")
    try:
        with opener.open(f"{url}/api/status", timeout=3) as response:
            json.load(response)
        print(f"Dashboard: {url}\nMode: Paper / Live via independent dashboard switches\nPID: {process.pid}\nLog: {log_path}")
        break
    except (OSError, ValueError):
        time.sleep(1)
else:
    raise SystemExit(f"Dashboard has not become ready yet (PID {process.pid}); inspect {log_path}")
PY
