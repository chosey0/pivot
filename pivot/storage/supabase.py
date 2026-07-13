"""Supabase 저수준 클라이언트 — PostgREST(메타데이터)와 Storage(객체) 분리.

repository들이 이 두 클라이언트만 사용한다. 애플리케이션은
`storage.objects` 테이블을 직접 만지지 않는다 (docs/06 §6).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from pivot.env import env_value

DATASET_BUCKET = "pivot-datasets"
MODEL_BUCKET = "pivot-models"
PARQUET_CONTENT_TYPE = "application/vnd.apache.parquet"


@dataclass(frozen=True)
class TrainingStorageConfig:
    url: str
    key: str

    @classmethod
    def from_env(cls) -> "TrainingStorageConfig":
        url = env_value("SUPABASE_URL").rstrip("/")
        key = env_value("SUPABASE_SERVICE_ROLE_KEY") or env_value("SUPABASE_SECRET_KEY")
        if not url or not key:
            raise RuntimeError("SUPABASE_URL and a server-side Supabase key are required")
        return cls(url=url, key=key)


def _raise_for_supabase(response: httpx.Response) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        try:
            detail: Any = response.json()
        except ValueError:
            detail = response.text
        raise RuntimeError(
            f"Supabase request failed: {response.status_code} {detail}"
        ) from exc


class PostgrestClient:
    """PostgREST 테이블 행 접근. filters는 PostgREST 문법 값 (예: {"id": "eq.3"})."""

    def __init__(
        self,
        config: TrainingStorageConfig | None = None,
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.config = config or TrainingStorageConfig.from_env()
        self._client = httpx.Client(
            base_url=f"{self.config.url}/rest/v1",
            headers={
                "apikey": self.config.key,
                "authorization": f"Bearer {self.config.key}",
                "content-type": "application/json",
            },
            timeout=timeout,
            transport=transport,
        )

    def select(
        self,
        table: str,
        *,
        filters: dict[str, str] | None = None,
        order: str | None = None,
        limit: int | None = None,
        columns: str = "*",
    ) -> list[dict]:
        params: dict[str, str] = {"select": columns, **(filters or {})}
        if order:
            params["order"] = order
        if limit is not None:
            params["limit"] = str(limit)
        response = self._client.get(f"/{table}", params=params)
        _raise_for_supabase(response)
        return response.json()

    def insert(self, table: str, rows: list[dict] | dict) -> list[dict]:
        payload = rows if isinstance(rows, list) else [rows]
        response = self._client.post(
            f"/{table}",
            json=payload,
            headers={"prefer": "return=representation"},
        )
        _raise_for_supabase(response)
        return response.json()

    def rpc(self, function: str, params: dict) -> list[dict]:
        response = self._client.post(f"/rpc/{function}", json=params)
        _raise_for_supabase(response)
        result = response.json()
        return result if isinstance(result, list) else [result]

    def update(
        self, table: str, values: dict, *, filters: dict[str, str]
    ) -> list[dict]:
        """filters에 걸린 행을 갱신하고 갱신된 행 목록을 반환한다 (0건일 수 있음)."""
        response = self._client.patch(
            f"/{table}",
            json=values,
            params=filters,
            headers={"prefer": "return=representation"},
        )
        _raise_for_supabase(response)
        return response.json()

    def delete(self, table: str, *, filters: dict[str, str]) -> None:
        if not filters:
            raise ValueError("delete requires filters")
        response = self._client.delete(f"/{table}", params=filters)
        _raise_for_supabase(response)


class StorageObjectClient:
    """private bucket 객체 접근. 불변 경로만 사용하며 덮어쓰지 않는다 (docs/06 §3)."""

    def __init__(
        self,
        config: TrainingStorageConfig | None = None,
        *,
        timeout: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.config = config or TrainingStorageConfig.from_env()
        self._client = httpx.Client(
            base_url=f"{self.config.url}/storage/v1",
            headers={
                "apikey": self.config.key,
                "authorization": f"Bearer {self.config.key}",
            },
            timeout=timeout,
            transport=transport,
        )

    def upload(
        self, bucket: str, path: str, data: bytes, *, content_type: str
    ) -> None:
        response = self._client.post(
            f"/object/{bucket}/{path}",
            content=data,
            headers={"content-type": content_type, "x-upsert": "false"},
        )
        _raise_for_supabase(response)

    def download(self, bucket: str, path: str) -> bytes:
        response = self._client.get(f"/object/{bucket}/{path}")
        _raise_for_supabase(response)
        return response.content

    def remove(self, bucket: str, paths: list[str]) -> None:
        if not paths:
            return
        response = self._client.request(
            "DELETE", f"/object/{bucket}", json={"prefixes": paths}
        )
        _raise_for_supabase(response)

    def list_objects(self, bucket: str, prefix: str, *, limit: int = 1000) -> list[dict]:
        response = self._client.post(
            f"/object/list/{bucket}",
            json={"prefix": prefix, "limit": limit},
            headers={"content-type": "application/json"},
        )
        _raise_for_supabase(response)
        return response.json()
