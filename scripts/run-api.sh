#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

cd "$ROOT_DIR"

if [[ -f ".env" ]]; then
  exec uv run --env-file .env uvicorn server.main:app --reload --host "$HOST" --port "$PORT"
fi

exec uv run uvicorn server.main:app --reload --host "$HOST" --port "$PORT"
