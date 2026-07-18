#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -f uvicorn.pid ] && kill -0 "$(cat uvicorn.pid)" 2>/dev/null; then
    echo "Server already running (PID $(cat uvicorn.pid))"
    exit 0
fi

source .worktrees/feat-core/.venv/bin/activate
set -a
source .env
set +a
export PYTHONUNBUFFERED=1

LOG_FILE="server_$(date +%Y%m%d_%H%M%S).log"

# Use logging.yaml for mac_system logs and redirect uvicorn stdout/stderr to a
# timestamped file so restarts do not overwrite the previous session's log.
nohup bash -c "exec uvicorn mac_system.api.main:app --host 0.0.0.0 --port 18080 --log-config logging.yaml" >"$LOG_FILE" 2>&1 &
echo $! > uvicorn.pid

echo "Server started (PID $(cat uvicorn.pid))"
echo "Logs: $(pwd)/$LOG_FILE"
