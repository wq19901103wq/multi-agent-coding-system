#!/usr/bin/env bash
# Install or refresh the independent watchdog LaunchAgent. Idempotent.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE_LABEL="com.mac-system.watchdog"
SERVICE_TARGET="gui/$(id -u)/$SERVICE_LABEL"
LAUNCH_AGENT_DIR="$HOME/Library/LaunchAgents"
LAUNCH_AGENT_PATH="$LAUNCH_AGENT_DIR/$SERVICE_LABEL.plist"
TEMPLATE_PATH="$PROJECT_ROOT/scripts/$SERVICE_LABEL.plist.template"

if [ "${1:-}" = "--uninstall" ]; then
    if launchctl print "$SERVICE_TARGET" >/dev/null 2>&1; then
        launchctl bootout "$SERVICE_TARGET"
    fi
    rm -f "$LAUNCH_AGENT_PATH"
    echo "Watchdog $SERVICE_LABEL uninstalled"
    exit 0
fi

mkdir -p "$LAUNCH_AGENT_DIR"
sed "s|__PROJECT_ROOT__|$PROJECT_ROOT|g" "$TEMPLATE_PATH" >"$LAUNCH_AGENT_PATH"
plutil -lint "$LAUNCH_AGENT_PATH" >/dev/null

if launchctl print "$SERVICE_TARGET" >/dev/null 2>&1; then
    launchctl bootout "$SERVICE_TARGET" 2>/dev/null || true
fi
for _ in $(seq 1 50); do
    if launchctl bootstrap "gui/$(id -u)" "$LAUNCH_AGENT_PATH" 2>/dev/null; then
        break
    fi
    if launchctl print "$SERVICE_TARGET" >/dev/null 2>&1; then
        break
    fi
    sleep 0.1
done
launchctl print "$SERVICE_TARGET" >/dev/null
echo "Watchdog $SERVICE_LABEL installed and running (logs: $PROJECT_ROOT/server_watchdog.log)"
