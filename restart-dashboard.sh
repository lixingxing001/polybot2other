#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8791}"
PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
LOG_DIR="${LOG_DIR:-$PROJECT_DIR/logs}"
LOG_FILE="${LOG_FILE:-$LOG_DIR/polybot2other-dashboard-${PORT}.log}"
FORCE_KILL_PORT="${FORCE_KILL_PORT:-0}"
HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-30}"

log() {
  printf '[restart-dashboard] %s\n' "$*"
}

die() {
  printf '[restart-dashboard] ERROR: %s\n' "$*" >&2
  exit 1
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

unique_pids() {
  awk 'NF && $1 ~ /^[0-9]+$/ && !seen[$1]++ { print $1 }'
}

pid_args() {
  ps -p "$1" -o args= 2>/dev/null || true
}

list_project_web_pids() {
  pgrep -af 'polybot2other[.]web' 2>/dev/null \
    | awk -v self="$$" -v parent="$PPID" '$1 != self && $1 != parent { print $1 }' \
    | unique_pids
}

list_port_pids() {
  {
    if have_cmd lsof; then
      lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true
    fi
    if have_cmd ss; then
      ss -ltnp 2>/dev/null \
        | awk -v port=":$PORT" '
          $4 ~ port "$" {
            line = $0
            while (match(line, /pid=[0-9]+/)) {
              print substr(line, RSTART + 4, RLENGTH - 4)
              line = substr(line, RSTART + RLENGTH)
            }
          }
        ' || true
    fi
    if have_cmd fuser; then
      fuser -n tcp "$PORT" 2>/dev/null | tr ' ' '\n' || true
    fi
  } | unique_pids
}

terminate_pids() {
  local reason="$1"
  shift || true
  local -a pids=()
  local -a alive=()
  local pid
  mapfile -t pids < <(printf '%s\n' "$@" | unique_pids)
  if (( ${#pids[@]} == 0 )); then
    return 0
  fi

  log "Stopping ${reason}: ${pids[*]}"
  kill "${pids[@]}" 2>/dev/null || true

  for _ in {1..20}; do
    alive=()
    for pid in "${pids[@]}"; do
      if kill -0 "$pid" 2>/dev/null; then
        alive+=("$pid")
      fi
    done
    if (( ${#alive[@]} == 0 )); then
      return 0
    fi
    sleep 0.25
  done

  log "Force stopping ${reason}: ${alive[*]}"
  kill -9 "${alive[@]}" 2>/dev/null || true
}

stop_project_services() {
  local -a pids=()
  mapfile -t pids < <(list_project_web_pids)
  if (( ${#pids[@]} == 0 )); then
    log "No existing polybot2other.web process found."
    return 0
  fi
  terminate_pids "polybot2other.web processes" "${pids[@]}"
}

stop_port_conflicts() {
  local -a pids=()
  local -a killable=()
  local -a blockers=()
  local pid args
  mapfile -t pids < <(list_port_pids)
  if (( ${#pids[@]} == 0 )); then
    return 0
  fi

  for pid in "${pids[@]}"; do
    args="$(pid_args "$pid")"
    if [[ "$FORCE_KILL_PORT" == "1" || "$args" == *"polybot2other.web"* || "$args" == *"$PROJECT_DIR"* ]]; then
      killable+=("$pid")
    else
      blockers+=("$pid|$args")
    fi
  done

  if (( ${#blockers[@]} > 0 )); then
    log "Port ${PORT} is occupied by non-project process(es):"
    for item in "${blockers[@]}"; do
      printf '  %s\n' "$item" >&2
    done
    die "Refusing to kill non-project process. Re-run with FORCE_KILL_PORT=1 only if you are sure."
  fi

  terminate_pids "processes listening on ${HOST}:${PORT}" "${killable[@]}"
}

assert_port_free() {
  local -a pids=()
  mapfile -t pids < <(list_port_pids)
  if (( ${#pids[@]} > 0 )); then
    log "Port ${PORT} is still occupied:"
    local pid
    for pid in "${pids[@]}"; do
      printf '  %s %s\n' "$pid" "$(pid_args "$pid")" >&2
    done
    die "Port ${PORT} is not free."
  fi
}

check_health() {
  local url="http://${HOST}:${PORT}/api/status"
  "$PYTHON_BIN" - "$url" <<'PY' >/dev/null 2>&1
import sys
import urllib.request

url = sys.argv[1]
with urllib.request.urlopen(url, timeout=2) as response:
    if response.status != 200:
        raise SystemExit(response.status)
PY
}

wait_for_health() {
  local deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
  while (( SECONDS < deadline )); do
    if check_health; then
      return 0
    fi
    sleep 1
  done
  return 1
}

main() {
  have_cmd rtk || die "rtk is not available in PATH."
  [[ -x "$PYTHON_BIN" ]] || die "Python runtime not found or not executable: $PYTHON_BIN"

  mkdir -p "$LOG_DIR"

  log "Project: $PROJECT_DIR"
  log "Target:  http://${HOST}:${PORT}"
  log "Log:     $LOG_FILE"

  stop_project_services
  stop_port_conflicts
  assert_port_free

  {
    printf '\n==== restart %s ====\n' "$(date '+%Y-%m-%d %H:%M:%S')"
  } >> "$LOG_FILE"

  log "Starting dashboard..."
  nohup rtk proxy setsid bash -c '
    log_file=$1
    project_dir=$2
    python_bin=$3
    host=$4
    port=$5
    cd "$project_dir"
    exec env PYTHONPATH=src "$python_bin" -m polybot2other.web --host "$host" --port "$port" >> "$log_file" 2>&1
  ' _ "$LOG_FILE" "$PROJECT_DIR" "$PYTHON_BIN" "$HOST" "$PORT" >/dev/null 2>&1 &
  local pid=$!
  log "Started wrapper PID: $pid"

  if wait_for_health; then
    log "Ready: http://${HOST}:${PORT}"
    log "Tail log: tail -f '$LOG_FILE'"
    return 0
  fi

  log "Health check failed. Recent log lines:"
  tail -n 80 "$LOG_FILE" >&2 || true
  die "Dashboard did not become healthy within ${HEALTH_TIMEOUT_SECONDS}s."
}

main "$@"
