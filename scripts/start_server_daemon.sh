#!/usr/bin/env bash
# Root counterpart of start_server.sh for the system-domain LaunchDaemon.
# Installs /Library/LaunchDaemons/com.mac-system.dev.plist and (re)starts the
# service in the system domain. The server process itself runs as the repo
# owner via the plist's UserName key.
set -euo pipefail

if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != --check ]; }; then
    echo 'Usage: start_server_daemon.sh [--check]' >&2
    exit 64
fi
if [ "$(id -u)" -ne 0 ]; then
    echo 'start_server_daemon.sh must run as root (sudo)' >&2
    exit 1
fi

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET_USER="$(stat -f %Su "$PROJECT_ROOT")"
TARGET_HOME="$(eval echo ~"$TARGET_USER")"
VENV_DIR="$PROJECT_ROOT/.venv"
if [ ! -d "$VENV_DIR" ]; then
    VENV_DIR="$PROJECT_ROOT/.worktrees/feat-core/.venv"
fi
SERVICE_LABEL="com.mac-system.dev"
SERVICE_TARGET="system/$SERVICE_LABEL"
DAEMON_PATH="/Library/LaunchDaemons/$SERVICE_LABEL.plist"
TEMPLATE_PATH="$PROJECT_ROOT/scripts/$SERVICE_LABEL.daemon.plist.template"

cd "$PROJECT_ROOT"
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

install_launch_daemon() {
    sed -e "s|__PROJECT_ROOT__|$PROJECT_ROOT|g" \
        -e "s|__USERNAME__|$TARGET_USER|g" \
        -e "s|__USER_HOME__|$TARGET_HOME|g" \
        "$TEMPLATE_PATH" >"$DAEMON_PATH"
    chown root:wheel "$DAEMON_PATH"
    chmod 644 "$DAEMON_PATH"
    plutil -lint "$DAEMON_PATH" >/dev/null
}

wait_for_health() {
    local expected_pid="$1"
    # Daemon handoff can exceed 120s under load: the outgoing server finishes
    # native-turn cleanup while the first daemon bind attempt may exit and be
    # relaunched by KeepAlive before the port is released.
    local deadline=$((SECONDS + 240))
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
    echo "Server did not become healthy within 120s under $SERVICE_TARGET; see $PROJECT_ROOT/server_launchctl.log" >&2
    echo "--- launchctl state ---" >&2
    launchctl print "$SERVICE_TARGET" 2>&1 | grep -E "state =|pid =|last exit code" >&2 || true
    echo "--- last server log lines ---" >&2
    tail -n 30 "$PROJECT_ROOT/server_launchctl.log" >&2 || true
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

install_launch_daemon
# launchd refuses to bootstrap a job whose label sits in the disabled list;
# restore the enabled state up front instead of retrying a refused bootstrap.
if launchctl print-disabled system 2>/dev/null \
    | awk -v label="\"$SERVICE_LABEL\"" '$1 == label && ($3 == "true" || $3 == "disabled") {found=1} END {exit !found}'; then
    launchctl enable "$SERVICE_TARGET" 2>/dev/null || true
fi
if service_loaded; then
    if ! launchctl kickstart "$SERVICE_TARGET"; then
        BOOTSTRAPPED=false
        for _ in $(seq 1 50); do
            if service_loaded; then
                if launchctl kickstart "$SERVICE_TARGET" 2>/dev/null; then
                    BOOTSTRAPPED=true
                    break
                fi
            elif launchctl bootstrap system "$DAEMON_PATH" 2>/dev/null; then
                BOOTSTRAPPED=true
                break
            fi
            sleep 0.1
        done
        if [ "$BOOTSTRAPPED" != true ]; then
            echo "Failed to restart launchd service $SERVICE_TARGET" >&2
            exit 1
        fi
    fi
else
    BOOTSTRAPPED=false
    BOOTSTRAP_ERR=""
    for _ in $(seq 1 50); do
        if BOOTSTRAP_ERR="$(launchctl bootstrap system "$DAEMON_PATH" 2>&1)"; then
            BOOTSTRAPPED=true
            break
        fi
        if service_loaded; then
            BOOTSTRAPPED=true
            break
        fi
        sleep 0.1
    done
    if [ "$BOOTSTRAPPED" != true ]; then
        echo "Failed to load launchd service $SERVICE_TARGET: ${BOOTSTRAP_ERR:-launchctl gave no error details}" >&2
        exit 1
    fi
fi

wait_for_health "$(service_pid || true)"
