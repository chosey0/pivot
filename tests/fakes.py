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
            actual = str(value).lower() if isinstance(value, bool) else str(value)
            if actual != expression[3:]:
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
        "diagnostic_reports": {"preset_id": None, "dataset_id": None},
        "training_runs": {
            "job_id": None,
            "status": "queued",
            "device": None,
            "best_epoch": None,
            "best_metric_name": None,
            "best_metric_value": None,
            "error": None,
            "started_at": None,
            "completed_at": None,
        },
        "live_deployments": {
            "active": True,
            "deactivated_at": None,
        },
    }
    ID_TABLES = (
        "jobs",
        "job_events",
        "training_presets",
        "datasets",
        "dataset_shards",
        "diagnostic_reports",
        "training_runs",
        "evaluations",
        "training_artifacts",
        "live_deployments",
    )

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

    def rpc(self, function: str, params: dict) -> list[dict]:
        if function != "activate_live_deployment":
            raise NotImplementedError(function)
        run_id = params["target_run_id"]
        artifact_id = params["target_artifact_id"]
        runs = [
            row
            for row in self.tables["training_runs"]
            if row["id"] == run_id and row["status"] == "succeeded"
        ]
        artifacts = [
            row
            for row in self.tables["training_artifacts"]
            if row["id"] == artifact_id
            and row["run_id"] == run_id
            and row["kind"] == "best_checkpoint"
        ]
        if not runs or not artifacts:
            raise RuntimeError("run/artifact is not deployable")
        for row in self.tables["live_deployments"]:
            if row["active"]:
                row["active"] = False
                row["deactivated_at"] = "2026-07-10T00:00:00+00:00"
        return self.insert(
            "live_deployments", {"run_id": run_id, "artifact_id": artifact_id}
        )

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
        elif table == "training_presets":
            preset_ids = {row["id"] for row in deleted}
            for report in self.tables["diagnostic_reports"]:
                if report.get("preset_id") in preset_ids:
                    report["preset_id"] = None
        elif table == "training_runs":
            run_ids = {row["id"] for row in deleted}
            for child in ("training_epochs", "evaluations", "training_artifacts"):
                self.tables[child] = [
                    row for row in self.tables[child] if row["run_id"] not in run_ids
                ]
            self.tables["live_deployments"] = [
                row
                for row in self.tables["live_deployments"]
                if row["run_id"] not in run_ids
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
        self.created_at: dict[tuple[str, str], str] = {}  # orphan age 시나리오용
        self.removed: list[list[str]] = []  # 삭제 순서 검증용 호출 기록

    def upload(self, bucket: str, path: str, data: bytes, *, content_type: str) -> None:
        key = (bucket, path)
        if key in self.objects:
            raise RuntimeError(f"object already exists (immutable path): {path}")
        self.objects[key] = data

    def download(self, bucket: str, path: str) -> bytes:
        data = self.objects[(bucket, path)]
        return data + b"corrupted" if path in self.corrupt_paths else data

    def remove(self, bucket: str, paths: list[str]) -> None:
        # Supabase처럼 존재하지 않는 경로는 조용히 건너뛴다 (재시도 멱등)
        self.removed.append(list(paths))
        for path in paths:
            self.objects.pop((bucket, path), None)

    def list_objects(self, bucket: str, prefix: str, *, limit: int = 1000) -> list[dict]:
        """Supabase list API 흉내 — 폴더 한 단계씩, 폴더는 id 없는 항목."""
        entries: dict[str, dict] = {}
        for (object_bucket, path) in self.objects:
            if object_bucket != bucket or not path.startswith(f"{prefix}/"):
                continue
            rest = path[len(prefix) + 1 :]
            head, separator, _ = rest.partition("/")
            if separator:
                entries[head] = {"name": head, "id": None}
            else:
                entries[head] = {
                    "name": head,
                    "id": head,
                    "created_at": self.created_at.get(
                        (bucket, path), "2020-01-01T00:00:00+00:00"
                    ),
                }
        return list(entries.values())[:limit]


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
