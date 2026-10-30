#!/usr/bin/env bash
# Watchdog entry: stdlib-only python, no venv required.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec /usr/bin/env python3 "$PROJECT_ROOT/scripts/service_watchdog.py" --root "$PROJECT_ROOT"
