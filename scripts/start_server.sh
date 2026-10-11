#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != --check ]; }; then
    echo 'Usage: start_server.sh [--check]' >&2
    exit 64
fi

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="$PROJECT_ROOT/.venv"
if [ ! -d "$VENV_DIR" ]; then
    VENV_DIR="$PROJECT_ROOT/.worktrees/feat-core/.venv"
fi
SERVICE_LABEL="com.mac-system.dev"
SERVICE_TARGET="gui/$(id -u)/$SERVICE_LABEL"
LAUNCH_AGENT_DIR="$HOME/Library/LaunchAgents"
LAUNCH_AGENT_PATH="$LAUNCH_AGENT_DIR/$SERVICE_LABEL.plist"
TEMPLATE_PATH="$PROJECT_ROOT/scripts/$SERVICE_LABEL.plist.template"

cd "$PROJECT_ROOT"
# Validate prerequisites before any process or launchd mutation. The restart
# entry also invokes this mode before it stops a currently healthy service.
for required in .env logging.yaml "$VENV_DIR/bin/activate" "$TEMPLATE_PATH"; do
    if [ ! -f "$required" ] || [ ! -r "$required" ]; then
        echo "Startup prerequisite missing or unreadable: $required" >&2
        exit 1
    fi
done
for required in "$VENV_DIR/bin/uvicorn" "$PROJECT_ROOT/scripts/run_server.sh"; do
    if [ ! -x "$required" ]; then
        echo "Startup executable unavailable: $required" >&2
        exit 1
    fi
done
bash -n .env
bash -n "$PROJECT_ROOT/scripts/run_server.sh"
plutil -lint "$TEMPLATE_PATH" >/dev/null
set -a
source .env
set +a
SERVER_PORT="${MCS_PORT:-18080}"
if [[ ! "$SERVER_PORT" =~ ^[0-9]{1,5}$ ]] || ((10#$SERVER_PORT < 1 || 10#$SERVER_PORT > 65535)); then
    echo 'MCS_PORT must be an integer between 1 and 65535' >&2
    exit 1
fi
if [ "${1:-}" = --check ]; then
    echo 'Startup prerequisites checked; no service action performed'
    exit 0
fi

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
    local deadline=$((SECONDS + 120))
    while [ "$SECONDS" -lt "$deadline" ]; do
        local running_pid
        running_pid="$(service_pid || true)"
        if [ -n "$running_pid" ] && [ "$running_pid" != "$expected_pid" ]; then
            expected_pid="$running_pid"
        fi
        if [ -n "$expected_pid" ] && ! kill -0 "$expected_pid" 2>/dev/null; then
            expected_pid=""
        fi
        if curl --silent --fail --max-time 1 \
            "http://127.0.0.1:${SERVER_PORT}/health" >/dev/null; then
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
    done
    echo "Server did not become healthy within 120s under $SERVICE_LABEL; see $PROJECT_ROOT/server_launchctl.log" >&2
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
    deadline=$((SECONDS + 120))
    while [ "$SECONDS" -lt "$deadline" ]; do
        kill -0 "$RUNNING_PID" 2>/dev/null || break
        sleep 0.1
    done
    if kill -0 "$RUNNING_PID" 2>/dev/null; then
        echo "Unmanaged server $RUNNING_PID has not exited; refusing launchd takeover" >&2
        exit 1
    fi
fi

install_launch_agent
if service_loaded; then
    # ``launchctl bootout`` returns before the label is always fully released.
    # A stop && start sequence can therefore observe the stale label here and
    # receive exit 37 from kickstart. Never use -k: a live process may simply
    # still be starting and must not be killed by another start invocation.
    if ! launchctl kickstart "$SERVICE_TARGET"; then
        BOOTSTRAPPED=false
        for _ in $(seq 1 50); do
            if service_loaded; then
                if launchctl kickstart "$SERVICE_TARGET" 2>/dev/null; then
                    BOOTSTRAPPED=true
                    break
                fi
            elif launchctl bootstrap "gui/$(id -u)" "$LAUNCH_AGENT_PATH" 2>/dev/null; then
                BOOTSTRAPPED=true
                break
            fi
            sleep 0.1
        done
        if [ "$BOOTSTRAPPED" != true ]; then
            echo "Failed to restart launchd service $SERVICE_LABEL" >&2
            exit 1
        fi
    fi
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
