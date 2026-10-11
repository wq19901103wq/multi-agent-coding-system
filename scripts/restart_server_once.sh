#!/usr/bin/env bash
# A deployment attempt may stop/start the service at most once. Verification
# is separate and read-only; a failed check must never replay this operation.
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OPERATION_ID="${1:-}"
if [ "$#" -ne 1 ] || [[ ! "$OPERATION_ID" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}$ ]]; then
    echo 'Usage: bash scripts/restart_server_once.sh <stable-authorized-operation-id>' >&2
    exit 64
fi
cd "$PROJECT_ROOT"
STATE_DIR="$PROJECT_ROOT/.mac/restart_operations"
OPERATION_DIR="$STATE_DIR/$OPERATION_ID"
LOCK_DIR="$STATE_DIR/.active-lock"
mkdir -p "$STATE_DIR"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "Restart blocked: another operation owns $LOCK_DIR; inspect it, do not auto-clear." >&2
    exit 75
fi
CLAIMED=false
finish() {
    local result=$?
    if [ "$CLAIMED" = true ] && [ "$result" -ne 0 ]; then
        echo "failed_or_interrupted exit=$result; inspect service state before any new authorization" > "$OPERATION_DIR/status"
    fi
    rmdir "$LOCK_DIR"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if ! mkdir "$OPERATION_DIR" 2>/dev/null; then
    echo "Restart blocked: operation $OPERATION_ID was already attempted. No service action performed. See $OPERATION_DIR/status" >&2
    exit 75
fi
CLAIMED=true
echo "claimed pid=$$ at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')" > "$OPERATION_DIR/status"
# Claim survives errors, signals and host restart. In particular never erase
# it merely because a health check or subsequent verification fails.
bash scripts/start_server.sh --check
bash scripts/stop_server.sh
echo 'stopped; starting' > "$OPERATION_DIR/status"
bash scripts/start_server.sh
echo 'restart_completed; independent verification still required' > "$OPERATION_DIR/status"
echo "Restart operation $OPERATION_ID completed; verify without replaying deployment."
