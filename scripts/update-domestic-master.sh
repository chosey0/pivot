#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$ROOT_DIR"

if [[ ! -f ".env" ]]; then
  echo "missing .env: SUPABASE_URL and SUPABASE_SECRET_KEY/SUPABASE_SERVICE_ROLE_KEY are required" >&2
  exit 1
fi

uv run --env-file .env python - <<'PY'
from pivot.symbols.master import load_domestic_common_stocks
from pivot.symbols.supabase import SupabaseDomesticMasterClient

print("KIS 국내 종목마스터 다운로드 중...")
entries = load_domestic_common_stocks()
print(f"정규화 완료: {len(entries):,}건")

client = SupabaseDomesticMasterClient(timeout=60.0)
print(f"Supabase public.{client.config.table} 업서트 중...")
upserted = client.upsert_entries(entries)

sample = client.search("005930", limit=1)
print(f"업서트 완료: {upserted:,}건")
if sample:
    item = sample[0]
    print(f"검색 확인: {item['symbol']} {item['name']} {item['market']}")
PY
