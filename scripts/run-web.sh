#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-5173}"

cd "$ROOT_DIR"

exec uv run python scripts/dev.py web --host "$HOST" --web-port "$PORT"
