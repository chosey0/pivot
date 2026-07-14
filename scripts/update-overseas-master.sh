#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$ROOT_DIR"

if [[ ! -f ".env" ]]; then
  echo "missing .env: SUPABASE_URL and SUPABASE_SECRET_KEY/SUPABASE_SERVICE_ROLE_KEY are required" >&2
  exit 1
fi

uv run --env-file .env python - <<'PY'
from pivot.symbols.master import load_us_symbol_master
from pivot.symbols.supabase import SupabaseOverseasMasterClient

print("KIS 미국 종목마스터 다운로드 중...")
entries = load_us_symbol_master()
print(f"정규화 완료: {len(entries):,}건")

client = SupabaseOverseasMasterClient(timeout=60.0)
print(f"Supabase public.{client.config.table} 동기화 중...")
synced = client.sync_entries(entries)
active = client.active_count()
if active != len(entries):
    raise RuntimeError(f"active row count mismatch: expected {len(entries)}, got {active}")
print(f"동기화 완료: {synced:,}건, active {active:,}건")
PY
