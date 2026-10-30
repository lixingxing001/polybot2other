#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$PROJECT_DIR/data/polybot2other-macos-8791.pid"
if [[ ! -f "$PID_FILE" ]]; then
  printf '%s\n' 'No Mac dashboard PID file found.'
  exit 0
fi
pid="$(cat "$PID_FILE")"
if [[ ! "$pid" =~ ^[0-9]+$ ]]; then
  printf '%s\n' 'Invalid dashboard PID file.' >&2
  exit 1
fi
if ! kill -0 "$pid" 2>/dev/null; then
  printf '%s\n' 'Dashboard is already stopped.'
  exit 0
fi
process_args="$(ps -p "$pid" -o command=)"
if [[ "$process_args" != *"$PROJECT_DIR/.venv/macos/bin/python -m polybot2other.web --host 127.0.0.1 --port 8791"* ]]; then
  printf '%s\n' 'PID belongs to a different process; refusing to stop it.' >&2
  exit 1
fi
kill -TERM "$pid"
for ((attempt = 0; attempt < 60; attempt++)); do
  if ! kill -0 "$pid" 2>/dev/null; then
    printf '%s\n' 'Dashboard stopped. Databases and settings are preserved.'
    exit 0
  fi
  sleep 0.5
done
printf 'Dashboard PID %s has not exited yet; inspect logs/polybot2other-macos-8791.log.\n' "$pid" >&2
exit 1
