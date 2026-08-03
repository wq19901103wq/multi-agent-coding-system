#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

VENV_DIR="$(pwd)/.worktrees/feat-core/.venv"
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi
SERVER_PORT="${MCS_PORT:-18080}"

is_mac_server() {
    local candidate_pid="$1"
    local command_line
    command_line="$(ps -p "$candidate_pid" -o command= 2>/dev/null || true)"
    [[
        ("$command_line" == *"$VENV_DIR/bin/uvicorn"* && "$command_line" == *"mac_system.api.main:app"*)
        || ("$command_line" == *".worktrees/feat-core/.venv/bin/python"* && "$command_line" == *"-m mac_system server"*)
    ]]
}

PID=""
if [ -f uvicorn.pid ]; then
    PID="$(cat uvicorn.pid)"
fi
if [ -z "$PID" ] || ! kill -0 "$PID" 2>/dev/null || ! is_mac_server "$PID"; then
    PID="$(lsof -nP -tiTCP:"$SERVER_PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)"
fi

if [ -z "$PID" ]; then
    echo "Server not running"
elif is_mac_server "$PID"; then
    kill "$PID"
    # Give FastAPI's lifespan shutdown enough time to cancel active Agent
    # subprocesses before another server instance is started.  Returning as
    # soon as TERM is sent used to leave an old Claude process editing a task
    # worktree while the replacement server recovered the same project.
    for _ in $(seq 1 150); do
        if ! kill -0 "$PID" 2>/dev/null; then
            break
        fi
        sleep 0.1
    done
    if kill -0 "$PID" 2>/dev/null; then
        echo "Server did not exit after 15s; forcing PID $PID to stop"
        kill -KILL "$PID"
    fi
    echo "Stopped server (PID $PID)"
else
    echo "Refusing to stop unrelated process $PID on port $SERVER_PORT" >&2
    exit 1
fi

rm -f uvicorn.pid
