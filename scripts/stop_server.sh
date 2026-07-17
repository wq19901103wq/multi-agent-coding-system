#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -f uvicorn.pid ]; then
    echo "No uvicorn.pid found"
    exit 0
fi

PID=$(cat uvicorn.pid)
if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
    echo "Stopped server (PID $PID)"
else
    echo "Server not running"
fi

rm -f uvicorn.pid
