#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
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
    local candidate_venv
    for candidate_venv in "$PROJECT_ROOT/.venv" "$PROJECT_ROOT/.worktrees/feat-core/.venv"; do
        if [[
            ("$command_line" == *"$candidate_venv/bin/uvicorn"* && "$command_line" == *"mac_system.api.main:app"*)
            || ("$command_line" == *"$candidate_venv/bin/python"* && "$command_line" == *"-m mac_system server"*)
        ]]; then
            return 0
        fi
    done
    return 1
}

PID="$(listener_pid || true)"
SERVICE_LOADED=false
RESTORE_ENABLED=false
if SERVICE_INFO="$(launchctl print "$SERVICE_TARGET" 2>/dev/null)"; then
    SERVICE_LOADED=true
    SERVICE_PID="$(awk '/^[[:space:]]*pid = [0-9]+$/ {print $3; exit}' <<<"$SERVICE_INFO")"
    if [ -n "$PID" ] && [ -n "$SERVICE_PID" ] && [ "$PID" != "$SERVICE_PID" ]; then
        echo "Refusing to stop: listener $PID differs from launchd process $SERVICE_PID" >&2
        exit 1
    fi
    PID="${SERVICE_PID:-$PID}"
fi
if [ -n "$PID" ]; then
    if ! is_mac_server "$PID"; then
        echo "Refusing to stop unrelated process $PID on port $SERVER_PORT" >&2
        exit 1
    fi
fi

# Older loaded jobs only allow 5s after bootout. Signal the validated process
# first, suppress KeepAlive relaunch, and unload only after cleanup completes.
# Restore the previous enabled state even if shutdown fails.
restore_launchd_state() {
    if [ "$RESTORE_ENABLED" = true ]; then
        launchctl enable "$SERVICE_TARGET"
    fi
}
trap restore_launchd_state EXIT
if [ "$SERVICE_LOADED" = true ]; then
    DISABLED_SERVICES="$(launchctl print-disabled "gui/$(id -u)")"
    if ! awk -v label="\"$SERVICE_LABEL\"" '$1 == label && ($3 == "true" || $3 == "disabled") {found=1} END {exit !found}' <<<"$DISABLED_SERVICES"; then
        launchctl disable "$SERVICE_TARGET"
        RESTORE_ENABLED=true
    fi
fi
if [ -n "$PID" ]; then
    kill -TERM "$PID" 2>/dev/null || true
fi

for _ in $(seq 1 1200); do
    # The listener closes before ASGI lifespan finishes native-turn cleanup.
    # Port release alone is not proof that the old writer has stopped.
    if [ -z "$PID" ] || ! kill -0 "$PID" 2>/dev/null; then
        break
    fi
    sleep 0.1
done

if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    echo "MAC server process $PID did not finish shutdown after 120s; refusing restart" >&2
    exit 1
fi
if [ "$SERVICE_LOADED" = true ]; then
    launchctl bootout "$SERVICE_TARGET"
fi
REMAINING_PID="$(listener_pid || true)"
if [ -n "$REMAINING_PID" ]; then
    echo "Port $SERVER_PORT is still occupied by process $REMAINING_PID; refusing restart" >&2
    exit 1
fi

rm -f uvicorn.pid
echo "Server stopped; launchd service $SERVICE_LABEL is unloaded"
