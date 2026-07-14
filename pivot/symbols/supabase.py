"""Supabase 국내 종목마스터 저장/검색 클라이언트."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from pivot.env import env_value
from pivot.symbols.master import US_MARKETS, DomesticMasterEntry, OverseasMasterEntry

DEFAULT_TABLE = "domestic_master"
OVERSEAS_TABLE = "overseas_master"


@dataclass(frozen=True)
class SupabaseConfig:
    url: str
    key: str
    table: str = DEFAULT_TABLE

    @classmethod
    def from_env(
        cls,
        *,
        table_env: str = "SUPABASE_DOMESTIC_TABLE",
        default_table: str = DEFAULT_TABLE,
    ) -> "SupabaseConfig":
        url = env_value("SUPABASE_URL").rstrip("/")
        key = (
            env_value("SUPABASE_SERVICE_ROLE_KEY")
            or env_value("SUPABASE_SECRET_KEY")
            or env_value("SUPABASE_KEY")
        )
        if not url or not key:
            raise RuntimeError("SUPABASE_URL and a server-side Supabase key are required")
        return cls(
            url=url,
            key=key,
            table=env_value(table_env) or default_table,
        )


class SupabaseDomesticMasterClient:
    def __init__(self, config: SupabaseConfig | None = None, *, timeout: float = 30.0) -> None:
        self.config = config or SupabaseConfig.from_env()
        self.timeout = timeout

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self.config.key,
            "authorization": f"Bearer {self.config.key}",
            "content-type": "application/json",
        }

    def upsert_entries(self, entries: list[DomesticMasterEntry], *, batch_size: int = 500) -> int:
        rows = [entry.to_supabase_row() for entry in entries]
        return self._upsert_rows(rows, on_conflict="symbol", batch_size=batch_size)

    def _upsert_rows(
        self,
        rows: list[dict[str, Any]],
        *,
        on_conflict: str,
        batch_size: int,
    ) -> int:
        total = 0
        for start in range(0, len(rows), batch_size):
            batch = rows[start : start + batch_size]
            response = httpx.post(
                f"{self.config.url}/rest/v1/{self.config.table}",
                params={"on_conflict": on_conflict},
                headers={
                    **self._headers,
                    "prefer": "resolution=merge-duplicates,return=minimal",
                },
                json=batch,
                timeout=self.timeout,
            )
            _raise_for_supabase(response)
            total += len(batch)
        return total

    def search(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        normalized = query.strip()
        if not normalized:
            return []
        response = httpx.post(
            f"{self.config.url}/rest/v1/rpc/search_domestic_master",
            headers=self._headers,
            json={"query": normalized, "match_limit": limit},
            timeout=self.timeout,
        )
        _raise_for_supabase(response)
        return response.json()


class SupabaseOverseasMasterClient(SupabaseDomesticMasterClient):
    def __init__(self, config: SupabaseConfig | None = None, *, timeout: float = 30.0) -> None:
        super().__init__(
            config
            or SupabaseConfig.from_env(
                table_env="SUPABASE_OVERSEAS_TABLE",
                default_table=OVERSEAS_TABLE,
            ),
            timeout=timeout,
        )

    def sync_entries(self, entries: list[OverseasMasterEntry], *, batch_size: int = 500) -> int:
        if {entry.market for entry in entries} != set(US_MARKETS):
            raise ValueError("a complete NASDAQ/NYSE/AMEX snapshot is required")
        updated_at = entries[0].updated_at
        if any(entry.updated_at != updated_at for entry in entries):
            raise ValueError("all overseas master entries must share one updated_at")

        total = self._upsert_rows(
            [entry.to_supabase_row() for entry in entries],
            on_conflict="market,symbol",
            batch_size=batch_size,
        )
        response = httpx.patch(
            f"{self.config.url}/rest/v1/{self.config.table}",
            params={"active": "eq.true", "updated_at": f"lt.{updated_at}"},
            headers={**self._headers, "prefer": "return=minimal"},
            json={"active": False},
            timeout=self.timeout,
        )
        _raise_for_supabase(response)
        return total

    def search(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        normalized = query.strip()
        if not normalized:
            return []
        response = httpx.post(
            f"{self.config.url}/rest/v1/rpc/search_overseas_master",
            headers=self._headers,
            json={"query": normalized, "match_limit": limit},
            timeout=self.timeout,
        )
        _raise_for_supabase(response)
        return response.json()

    def active_count(self) -> int:
        response = httpx.get(
            f"{self.config.url}/rest/v1/{self.config.table}",
            params={"select": "symbol", "active": "eq.true"},
            headers={**self._headers, "prefer": "count=exact", "range": "0-0"},
            timeout=self.timeout,
        )
        _raise_for_supabase(response)
        return int(response.headers["content-range"].rsplit("/", 1)[1])


def _raise_for_supabase(response: httpx.Response) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = _error_detail(response)
        raise RuntimeError(f"Supabase request failed: {response.status_code} {detail}") from exc


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text
    code = payload.get("code")
    message = payload.get("message", "")
    if code == "PGRST202":
        return "search RPC is missing; apply pending Supabase migrations first"
    if code == "42P01":
        return "symbol master table is missing; apply pending Supabase migrations first"
    return str(message or payload)
