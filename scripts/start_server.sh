#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="$PROJECT_ROOT/.worktrees/feat-core/.venv"
SERVICE_LABEL="com.mac-system.dev"
SERVICE_TARGET="gui/$(id -u)/$SERVICE_LABEL"
LAUNCH_AGENT_DIR="$HOME/Library/LaunchAgents"
LAUNCH_AGENT_PATH="$LAUNCH_AGENT_DIR/$SERVICE_LABEL.plist"
TEMPLATE_PATH="$PROJECT_ROOT/scripts/$SERVICE_LABEL.plist.template"

cd "$PROJECT_ROOT"
set -a
source .env
set +a
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

service_loaded() {
    launchctl print "$SERVICE_TARGET" >/dev/null 2>&1
}

service_pid() {
    launchctl print "$SERVICE_TARGET" 2>/dev/null \
        | awk '/^[[:space:]]*pid = [0-9]+$/ {print $3; exit}'
}

install_launch_agent() {
    mkdir -p "$LAUNCH_AGENT_DIR"
    sed "s|__PROJECT_ROOT__|$PROJECT_ROOT|g" "$TEMPLATE_PATH" >"$LAUNCH_AGENT_PATH"
    plutil -lint "$LAUNCH_AGENT_PATH" >/dev/null
}

wait_for_health() {
    local expected_pid="$1"
    local attempt=0
    while [ "$attempt" -lt 300 ]; do
        local running_pid
        running_pid="$(service_pid || true)"
        if [ -n "$running_pid" ] && [ "$running_pid" != "$expected_pid" ]; then
            expected_pid="$running_pid"
        fi
        if [ -n "$expected_pid" ] && ! kill -0 "$expected_pid" 2>/dev/null; then
            expected_pid=""
        fi
        if curl --silent --fail --max-time 1 \
            "http://127.0.0.1:${SERVER_PORT}/admin/projects" >/dev/null; then
            local bound_pid
            bound_pid="$(listener_pid || true)"
            if [ -n "$bound_pid" ] && [ "$bound_pid" = "$expected_pid" ]; then
                echo "$bound_pid" >uvicorn.pid
                echo "Server started under launchd (PID $bound_pid, port $SERVER_PORT)"
                echo "Logs: $PROJECT_ROOT/server_launchctl.log"
                return 0
            fi
        fi
        sleep 0.1
        attempt=$((attempt + 1))
    done
    echo "Server did not become healthy under $SERVICE_LABEL; see $PROJECT_ROOT/server_launchctl.log" >&2
    return 1
}

RUNNING_PID="$(listener_pid || true)"
LAUNCHD_PID="$(service_pid || true)"
if [ -n "$RUNNING_PID" ]; then
    if ! is_mac_server "$RUNNING_PID"; then
        echo "Port $SERVER_PORT is occupied by an unrelated process (PID $RUNNING_PID)" >&2
        exit 1
    fi
    if [ -n "$LAUNCHD_PID" ] && [ "$RUNNING_PID" = "$LAUNCHD_PID" ]; then
        echo "$RUNNING_PID" >uvicorn.pid
        echo "Server already running under launchd (PID $RUNNING_PID, port $SERVER_PORT)"
        exit 0
    fi
    echo "Stopping unmanaged MAC server (PID $RUNNING_PID) before launchd takeover"
    kill "$RUNNING_PID"
    for _ in $(seq 1 150); do
        kill -0 "$RUNNING_PID" 2>/dev/null || break
        sleep 0.1
    done
fi

install_launch_agent
if service_loaded; then
    launchctl kickstart -k "$SERVICE_TARGET"
else
    BOOTSTRAPPED=false
    for _ in $(seq 1 50); do
        if launchctl bootstrap "gui/$(id -u)" "$LAUNCH_AGENT_PATH" 2>/dev/null; then
            BOOTSTRAPPED=true
            break
        fi
        # launchd may report the job unloaded before a preceding bootout has
        # completely released its label. Retry that bounded handoff instead
        # of leaving the service silently stopped after stop && start.
        if service_loaded; then
            BOOTSTRAPPED=true
            break
        fi
        sleep 0.1
    done
    if [ "$BOOTSTRAPPED" != true ]; then
        echo "Failed to load launchd service $SERVICE_LABEL" >&2
        exit 1
    fi
fi

wait_for_health "$(service_pid || true)"
