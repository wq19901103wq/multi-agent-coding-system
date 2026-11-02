#!/usr/bin/env bash
# Deploy the MAC server + watchdog as system LaunchDaemons so they survive
# GUI session logout/crash. Idempotent. Use --rollback to restore the
# GUI LaunchAgent setup.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    exec sudo "$0" "$@"
fi

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET_USER="$(stat -f %Su "$PROJECT_ROOT")"
TARGET_UID="$(id -u "$TARGET_USER")"
TARGET_HOME="$(eval echo ~"$TARGET_USER")"
SERVER_LABEL="com.mac-system.dev"
WATCHDOG_LABEL="com.mac-system.watchdog"
GUI_AGENT_DIR="$TARGET_HOME/Library/LaunchAgents"
DAEMON_DIR="/Library/LaunchDaemons"

if [ "${1:-}" = "--rollback" ]; then
    for label in "$WATCHDOG_LABEL" "$SERVER_LABEL"; do
        if launchctl print "system/$label" >/dev/null 2>&1; then
            launchctl bootout "system/$label" 2>/dev/null || true
        fi
        rm -f "$DAEMON_DIR/$label.plist"
    done
    sudo -u "$TARGET_USER" "$PROJECT_ROOT/scripts/install_watchdog.sh"
    sudo -u "$TARGET_USER" "$PROJECT_ROOT/scripts/start_server.sh"
    echo "Rollback complete: GUI LaunchAgent setup restored"
    exit 0
fi

# 1. Stop GUI watchdog first so it cannot race the server stop.
if launchctl print "gui/$TARGET_UID/$WATCHDOG_LABEL" >/dev/null 2>&1; then
    launchctl bootout "gui/$TARGET_UID/$WATCHDOG_LABEL" 2>/dev/null || true
fi
# 2. Gracefully stop the GUI server (queues interrupted turns for recovery).
if launchctl print "gui/$TARGET_UID/$SERVER_LABEL" >/dev/null 2>&1; then
    sudo -u "$TARGET_USER" "$PROJECT_ROOT/scripts/stop_server.sh"
fi
# 3. Remove GUI plists so a later login does not re-load them.
rm -f "$GUI_AGENT_DIR/$SERVER_LABEL.plist" "$GUI_AGENT_DIR/$WATCHDOG_LABEL.plist"

# 4. Install and start the server daemon (runs as the repo owner).
"$PROJECT_ROOT/scripts/start_server_daemon.sh"

# 5. Install and start the watchdog daemon (root; can bootstrap system jobs).
sed -e "s|__PROJECT_ROOT__|$PROJECT_ROOT|g" \
    -e "s|__USER_HOME__|$TARGET_HOME|g" \
    "$PROJECT_ROOT/scripts/$WATCHDOG_LABEL.daemon.plist.template" \
    >"$DAEMON_DIR/$WATCHDOG_LABEL.plist"
chown root:wheel "$DAEMON_DIR/$WATCHDOG_LABEL.plist"
chmod 644 "$DAEMON_DIR/$WATCHDOG_LABEL.plist"
plutil -lint "$DAEMON_DIR/$WATCHDOG_LABEL.plist" >/dev/null
if launchctl print "system/$WATCHDOG_LABEL" >/dev/null 2>&1; then
    launchctl bootout "system/$WATCHDOG_LABEL" 2>/dev/null || true
fi
BOOTSTRAPPED=false
for _ in $(seq 1 50); do
    if launchctl bootstrap system "$DAEMON_DIR/$WATCHDOG_LABEL.plist" 2>/dev/null; then
        BOOTSTRAPPED=true
        break
    fi
    if launchctl print "system/$WATCHDOG_LABEL" >/dev/null 2>&1; then
        BOOTSTRAPPED=true
        break
    fi
    sleep 0.1
done
if [ "$BOOTSTRAPPED" != true ]; then
    echo "Failed to load watchdog daemon system/$WATCHDOG_LABEL" >&2
    exit 1
fi

launchctl print "system/$SERVER_LABEL" >/dev/null
launchctl print "system/$WATCHDOG_LABEL" >/dev/null
echo "Daemon stack deployed: server + watchdog run in the system domain (survive logout/crash)"
echo "Rollback: sudo $PROJECT_ROOT/scripts/deploy_daemon_stack.sh --rollback"
