#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

cd "$ROOT_DIR"

if [[ -f ".env" ]]; then
  exec uv run --extra server --extra train --env-file .env python scripts/dev.py api --host "$HOST" --api-port "$PORT"
fi

exec uv run --extra server --extra train python scripts/dev.py api --host "$HOST" --api-port "$PORT"
