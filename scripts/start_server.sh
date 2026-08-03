#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

VENV_DIR="$(pwd)/.worktrees/feat-core/.venv"
source "$VENV_DIR/bin/activate"
set -a
source .env
set +a
export PYTHONUNBUFFERED=1

LOG_FILE="server_$(date +%Y%m%d_%H%M%S).log"
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

# A PID file is only advisory.  A prior crash can leave it pointing at a reused
# PID, while an older server may still own the port.  Resolve the real listener
# before starting so a healthy response from the old process cannot be mistaken
# for successful startup of the new code.
RUNNING_PID="$(listener_pid || true)"
if [ -n "$RUNNING_PID" ]; then
    if is_mac_server "$RUNNING_PID"; then
        echo "$RUNNING_PID" > uvicorn.pid
        echo "Server already running (PID $RUNNING_PID, port $SERVER_PORT)"
        exit 0
    fi
    echo "Port $SERVER_PORT is occupied by an unrelated process (PID $RUNNING_PID)" >&2
    exit 1
fi
rm -f uvicorn.pid

# Use logging.yaml for mac_system logs and redirect uvicorn stdout/stderr to a
# timestamped file so restarts do not overwrite the previous session's log.
nohup "$VENV_DIR/bin/uvicorn" mac_system.api.main:app \
    --host 0.0.0.0 --port "$SERVER_PORT" --log-config logging.yaml \
    </dev/null >"$LOG_FILE" 2>&1 &
SERVER_PID=$!
echo "$SERVER_PID" > uvicorn.pid

# Startup includes stale-task recovery and the Feishu WebSocket bootstrap.  Five
# seconds is too short on a repository with history and can kill a healthy
# process while it is still initializing.  Keep each probe short, but allow the
# service up to 30 seconds to become ready.
for ((attempt = 0; attempt < 300; attempt++)); do
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "Server exited during startup; see $(pwd)/$LOG_FILE" >&2
        rm -f uvicorn.pid
        exit 1
    fi
    if curl --silent --fail --max-time 1 "http://127.0.0.1:${SERVER_PORT}/admin/projects" >/dev/null; then
        break
    fi
    sleep 0.1
done

BOUND_PID="$(listener_pid || true)"
if [ "$BOUND_PID" != "$SERVER_PID" ]; then
    echo "Server process $SERVER_PID does not own port $SERVER_PORT (listener: ${BOUND_PID:-none})" >&2
    kill "$SERVER_PID" 2>/dev/null || true
    rm -f uvicorn.pid
    exit 1
fi

if ! curl --silent --fail --max-time 2 "http://127.0.0.1:${SERVER_PORT}/admin/projects" >/dev/null; then
    echo "Server did not become healthy; see $(pwd)/$LOG_FILE" >&2
    kill "$SERVER_PID" 2>/dev/null || true
    rm -f uvicorn.pid
    exit 1
fi

echo "Server started (PID $SERVER_PID, port $SERVER_PORT)"
echo "Logs: $(pwd)/$LOG_FILE"
