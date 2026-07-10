"""테스트용 인메모리 Supabase 대역.

FakeDb는 PostgrestClient와 같은 메서드/필터 문법(부분집합: eq/is.null/in/gt)을
구현해 실제 repository 코드를 그대로 검증한다. FakeStorage는 불변 업로드
계약(같은 경로 덮어쓰기 금지)을 흉내 낸다.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy

import numpy as np
import pandas as pd


def _match(row: dict, filters: dict[str, str]) -> bool:
    for column, expression in filters.items():
        value = row.get(column)
        if expression == "is.null":
            if value is not None:
                return False
        elif expression.startswith("eq."):
            if str(value) != expression[3:]:
                return False
        elif expression.startswith("gt."):
            if value is None or not float(value) > float(expression[3:]):
                return False
        elif expression.startswith("in.(") and expression.endswith(")"):
            if str(value) not in expression[4:-1].split(","):
                return False
        else:
            raise NotImplementedError(f"filter {expression!r}")
    return True


class FakeDb:
    """docs/06 스키마의 기본값을 재현하는 인메모리 PostgREST."""

    DEFAULTS: dict[str, dict] = {
        "jobs": {
            "status": "queued",
            "payload": {},
            "result": None,
            "error": None,
            "completed_items": 0,
            "total_items": 0,
            "started_at": None,
            "completed_at": None,
        },
        "training_presets": {"archived_at": None},
        "datasets": {
            "status": "building",
            "sample_count": 0,
            "symbol_count": 0,
            "class_counts": {},
            "failure_message": None,
            "completed_at": None,
        },
        "dataset_symbols": {
            "split": None,
            "status": "pending",
            "sample_count": 0,
            "class_counts": {},
            "length_stats": {},
            "error": None,
        },
    }
    ID_TABLES = ("jobs", "job_events", "training_presets", "datasets", "dataset_shards")

    def __init__(self) -> None:
        self.tables: dict[str, list[dict]] = defaultdict(list)
        self._next_id: dict[str, int] = defaultdict(lambda: 1)

    def select(
        self,
        table: str,
        *,
        filters: dict[str, str] | None = None,
        order: str | None = None,
        limit: int | None = None,
        columns: str = "*",
    ) -> list[dict]:
        rows = [deepcopy(row) for row in self.tables[table] if _match(row, filters or {})]
        if order:
            for clause in reversed(order.split(",")):
                column, _, direction = clause.partition(".")
                rows.sort(key=lambda row: row.get(column), reverse=direction == "desc")
        if limit is not None:
            rows = rows[:limit]
        if columns != "*":
            wanted = [name.strip() for name in columns.split(",")]
            rows = [{name: row.get(name) for name in wanted} for row in rows]
        return rows

    def insert(self, table: str, rows: list[dict] | dict) -> list[dict]:
        payload = rows if isinstance(rows, list) else [rows]
        inserted = []
        for row in payload:
            record = {**deepcopy(self.DEFAULTS.get(table, {})), **deepcopy(row)}
            if table in self.ID_TABLES and "id" not in record:
                record["id"] = self._next_id[table]
                self._next_id[table] += 1
            record.setdefault("created_at", "2026-07-10T00:00:00+00:00")
            self.tables[table].append(record)
            inserted.append(deepcopy(record))
        return inserted

    def update(self, table: str, values: dict, *, filters: dict[str, str]) -> list[dict]:
        updated = []
        for row in self.tables[table]:
            if _match(row, filters):
                row.update(deepcopy(values))
                updated.append(deepcopy(row))
        return updated

    def delete(self, table: str, *, filters: dict[str, str]) -> None:
        if not filters:
            raise ValueError("delete requires filters")
        deleted = [row for row in self.tables[table] if _match(row, filters)]
        self.tables[table] = [
            row for row in self.tables[table] if not _match(row, filters)
        ]
        if table == "datasets":
            dataset_ids = {row["id"] for row in deleted}
            self.tables["dataset_symbols"] = [
                row
                for row in self.tables["dataset_symbols"]
                if row["dataset_id"] not in dataset_ids
            ]
            self.tables["dataset_shards"] = [
                row
                for row in self.tables["dataset_shards"]
                if row["dataset_id"] not in dataset_ids
            ]
        elif table == "jobs":
            job_ids = {row["id"] for row in deleted}
            self.tables["job_events"] = [
                row for row in self.tables["job_events"] if row["job_id"] not in job_ids
            ]


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.corrupt_paths: set[str] = set()  # 검증 실패 시나리오용

    def upload(self, bucket: str, path: str, data: bytes, *, content_type: str) -> None:
        key = (bucket, path)
        if key in self.objects:
            raise RuntimeError(f"object already exists (immutable path): {path}")
        self.objects[key] = data

    def download(self, bucket: str, path: str) -> bytes:
        data = self.objects[(bucket, path)]
        return data + b"corrupted" if path in self.corrupt_paths else data


def make_candles(length: int = 240, seed: int = 3) -> pd.DataFrame:
    """프랙탈 지점이 충분히 생기는 무작위 캔들 (Time 인덱스 표준 스키마)."""
    rng = np.random.default_rng(seed)
    highs = rng.uniform(100, 120, length)
    lows = highs - rng.uniform(1, 3, length)
    close = (highs + lows) / 2
    index = pd.date_range("2025-01-01", periods=length, freq="D", name="Time")
    return pd.DataFrame(
        {
            "Open": close,
            "High": highs,
            "Low": lows,
            "Close": close,
            "Volume": 1000,
            "Amount": 1_000_000_000,
        },
        index=index,
    )
