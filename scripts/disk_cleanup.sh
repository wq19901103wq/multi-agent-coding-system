#!/usr/bin/env bash
# Periodic disk cleanup: removes old digital-human disk-history cache files
# only when disk usage is at or above the threshold. Idempotent, stdlib only.
set -euo pipefail

THRESHOLD_PERCENT="${DISK_CLEANUP_THRESHOLD:-90}"
RETENTION_DAYS="${DISK_CLEANUP_RETENTION_DAYS:-14}"
USER_HOME="${DISK_CLEANUP_HOME:-$HOME}"
CLEANUP_ROOT="$USER_HOME/digital-human/instances"

LOG_FILE="$(cd "$(dirname "$0")/.." && pwd)/disk_cleanup.log"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >>"$LOG_FILE"
}

PERCENT="$(df -P "$USER_HOME" | awk 'NR==2 {gsub(/%/, "", $5); print $5}')"
log "disk usage=${PERCENT}% threshold=${THRESHOLD_PERCENT}% retention=${RETENTION_DAYS}d"

if [ "$PERCENT" -lt "$THRESHOLD_PERCENT" ]; then
    exit 0
fi

freed_bytes=0
deleted=0
for dir in "$CLEANUP_ROOT"/*/data/.cache/disk-history; do
    [ -d "$dir" ] || continue
    while IFS= read -r -d '' file; do
        size=$(stat -f %z "$file" 2>/dev/null || echo 0)
        if rm -f "$file"; then
            freed_bytes=$((freed_bytes + size))
            deleted=$((deleted + 1))
        fi
    done < <(find "$dir" -type f -name "*.sqlite3" -mtime "+$RETENTION_DAYS" -print0)
done

freed_mb=$((freed_bytes / 1048576))
log "cleanup done: deleted=${deleted} files freed=${freed_mb}M new_usage=$(df -P "$USER_HOME" | awk 'NR==2 {gsub(/%/, "", $5); print $5}')%"
