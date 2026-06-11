#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8791}"
FORCE_KILL_PORT="${FORCE_KILL_PORT:-0}"
STOP_GRACE_SECONDS="${STOP_GRACE_SECONDS:-5}"

log() {
  printf '[stop-dashboard] %s\n' "$*"
}

die() {
  printf '[stop-dashboard] ERROR: %s\n' "$*" >&2
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
  local checks
  mapfile -t pids < <(printf '%s\n' "$@" | unique_pids)
  if (( ${#pids[@]} == 0 )); then
    return 0
  fi

  log "Stopping ${reason}: ${pids[*]}"
  kill "${pids[@]}" 2>/dev/null || true

  checks=$((STOP_GRACE_SECONDS * 4))
  if (( checks < 1 )); then
    checks=1
  fi
  for (( _ = 0; _ < checks; _++ )); do
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
    die "Refusing to kill non-project process. Set FORCE_KILL_PORT=1 only after checking the PID."
  fi

  terminate_pids "processes listening on ${HOST}:${PORT}" "${killable[@]}"
}

assert_port_free() {
  local -a pids=()
  local pid
  mapfile -t pids < <(list_port_pids)
  if (( ${#pids[@]} > 0 )); then
    log "Port ${PORT} is still occupied:"
    for pid in "${pids[@]}"; do
      printf '  %s %s\n' "$pid" "$(pid_args "$pid")" >&2
    done
    die "Port ${PORT} is not free."
  fi
}

main() {
  log "Project: $PROJECT_DIR"
  log "Target:  http://${HOST}:${PORT}"
  log "Grace:   ${STOP_GRACE_SECONDS}s"

  # 先按项目进程停，再检查端口，避免误杀同机其它服务。
  stop_project_services
  stop_port_conflicts
  assert_port_free

  log "Stopped: http://${HOST}:${PORT}"
}

main "$@"
