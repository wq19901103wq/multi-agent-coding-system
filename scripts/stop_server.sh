#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="$PROJECT_ROOT/.worktrees/feat-core/.venv"
SERVICE_LABEL="com.mac-system.dev"
SERVICE_TARGET="gui/$(id -u)/$SERVICE_LABEL"

cd "$PROJECT_ROOT"
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi
SERVER_PORT="${MCS_PORT:-18080}"

listener_pid() {
    lsof -nP -tiTCP:"$SERVER_PORT" -sTCP:LISTEN 2>/dev/null | head -1
}

is_mac_server() {
    local candidate_pid="$1"
    local command_line
    command_line="$(ps -p "$candidate_pid" -o command= 2>/dev/null || true)"
    [[
        ("$command_line" == *"$VENV_DIR/bin/uvicorn"* && "$command_line" == *"mac_system.api.main:app"*)
        || ("$command_line" == *".worktrees/feat-core/.venv/bin/python"* && "$command_line" == *"-m mac_system server"*)
    ]]
}

if launchctl print "$SERVICE_TARGET" >/dev/null 2>&1; then
    launchctl bootout "$SERVICE_TARGET"
fi

PID="$(listener_pid || true)"
if [ -n "$PID" ]; then
    if ! is_mac_server "$PID"; then
        echo "Refusing to stop unrelated process $PID on port $SERVER_PORT" >&2
        exit 1
    fi
    kill "$PID" 2>/dev/null || true
fi

for _ in $(seq 1 150); do
    PID="$(listener_pid || true)"
    [ -z "$PID" ] && break
    sleep 0.1
done

PID="$(listener_pid || true)"
if [ -n "$PID" ]; then
    echo "MAC server did not release port $SERVER_PORT after 15s" >&2
    exit 1
fi

rm -f uvicorn.pid
echo "Server stopped; launchd service $SERVICE_LABEL is unloaded"
