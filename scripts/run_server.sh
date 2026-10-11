#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="$PROJECT_ROOT/.venv"
if [ ! -d "$VENV_DIR" ]; then
    VENV_DIR="$PROJECT_ROOT/.worktrees/feat-core/.venv"
fi

cd "$PROJECT_ROOT"
source "$VENV_DIR/bin/activate"
set -a
source .env
set +a

# GUI launchd jobs do not inherit the interactive shell's PATH. Keep the
# coding-agent CLIs discoverable without duplicating their selection logic.
export PATH="$HOME/.local/node/bin:$HOME/.local/bin:$PATH"
export PYTHONUNBUFFERED=1

exec "$VENV_DIR/bin/uvicorn" mac_system.api.main:app \
    --host "${MCS_HOST:-127.0.0.1}" --port "${MCS_PORT:-18080}" --log-config logging.yaml
