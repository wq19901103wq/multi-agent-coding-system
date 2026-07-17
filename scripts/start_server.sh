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
export FEISHU_TEST_MODE=1
export PYTHONUNBUFFERED=1

# Use logging.yaml for append + rotation; stdout/stderr can be discarded.
nohup bash -c 'exec uvicorn mac_system.api.main:app --host 0.0.0.0 --port 18080 --log-config logging.yaml' >/dev/null 2>&1 &
echo $! > uvicorn.pid

echo "Server started (PID $(cat uvicorn.pid))"
echo "Logs: $(pwd)/server.log"
