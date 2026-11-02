#!/usr/bin/env bash
# Install or refresh the disk cleanup job. Default: LaunchDaemon (system domain,
# runs even when no user is logged in). Use --gui for a GUI-session LaunchAgent.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE_LABEL="com.mac-system.disk-cleanup"
# Derive the service user from the repo owner: this script is also reached via
# sudo, where SUDO_USER/USER and $HOME point at the wrong account.
EFFECTIVE_USER="$(stat -f %Su "$PROJECT_ROOT")"
EFFECTIVE_UID="$(id -u "$EFFECTIVE_USER")"
USER_HOME_DIR="$(eval echo ~"$EFFECTIVE_USER")"
GUI_TARGET="gui/$EFFECTIVE_UID/$SERVICE_LABEL"
SYSTEM_TARGET="system/$SERVICE_LABEL"
GUI_PLIST="$USER_HOME_DIR/Library/LaunchAgents/$SERVICE_LABEL.plist"
DAEMON_PLIST="/Library/LaunchDaemons/$SERVICE_LABEL.plist"
DAEMON_TEMPLATE="$PROJECT_ROOT/scripts/$SERVICE_LABEL.daemon.plist.template"
GUI_TEMPLATE="$PROJECT_ROOT/scripts/$SERVICE_LABEL.plist.template"

uninstall_gui() {
    if launchctl print "$GUI_TARGET" >/dev/null 2>&1; then
        launchctl bootout "$GUI_TARGET" 2>/dev/null || true
    fi
    rm -f "$GUI_PLIST"
}

uninstall_daemon() {
    if launchctl print "$SYSTEM_TARGET" >/dev/null 2>&1; then
        launchctl bootout "$SYSTEM_TARGET" 2>/dev/null || true
    fi
    rm -f "$DAEMON_PLIST"
}

if [ "${1:-}" = "--uninstall" ]; then
    if [ "$(id -u)" -ne 0 ]; then
        exec sudo "$0" --uninstall
    fi
    uninstall_daemon
    uninstall_gui
    echo "Disk cleanup $SERVICE_LABEL fully uninstalled"
    exit 0
fi

if [ "${1:-}" = "--gui" ]; then
    uninstall_daemon
    mkdir -p "$USER_HOME_DIR/Library/LaunchAgents"
    sed "s|__PROJECT_ROOT__|$PROJECT_ROOT|g" "$GUI_TEMPLATE" >"$GUI_PLIST"
    plutil -lint "$GUI_PLIST" >/dev/null
    if launchctl print "$GUI_TARGET" >/dev/null 2>&1; then
        launchctl bootout "$GUI_TARGET" 2>/dev/null || true
    fi
    for _ in $(seq 1 50); do
        if launchctl bootstrap "gui/$EFFECTIVE_UID" "$GUI_PLIST" 2>/dev/null; then
            break
        fi
        if launchctl print "$GUI_TARGET" >/dev/null 2>&1; then
            break
        fi
        sleep 0.1
    done
    launchctl print "$GUI_TARGET" >/dev/null
    echo "Disk cleanup $SERVICE_LABEL installed as GUI LaunchAgent (logs: $PROJECT_ROOT/disk_cleanup.log)"
    exit 0
fi

# Default: LaunchDaemon (requires root).
if [ "$(id -u)" -ne 0 ]; then
    exec sudo "$0" "$@"
fi

uninstall_gui
sed -e "s|__PROJECT_ROOT__|$PROJECT_ROOT|g" \
    -e "s|__USERNAME__|yihanwang|g" \
    -e "s|__USER_HOME__|/Users/yihanwang|g" \
    "$DAEMON_TEMPLATE" >"$DAEMON_PLIST"
chown root:wheel "$DAEMON_PLIST"
chmod 644 "$DAEMON_PLIST"
plutil -lint "$DAEMON_PLIST" >/dev/null

if launchctl print "$SYSTEM_TARGET" >/dev/null 2>&1; then
    launchctl bootout "$SYSTEM_TARGET" 2>/dev/null || true
fi
for _ in $(seq 1 50); do
    if launchctl bootstrap system "$DAEMON_PLIST" 2>/dev/null; then
        break
    fi
    if launchctl print "$SYSTEM_TARGET" >/dev/null 2>&1; then
        break
    fi
    sleep 0.1
done
launchctl print "$SYSTEM_TARGET" >/dev/null
echo "Disk cleanup $SERVICE_LABEL installed as LaunchDaemon (runs without login; logs: $PROJECT_ROOT/disk_cleanup.log)"
