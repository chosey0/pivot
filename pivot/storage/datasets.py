"""datasets/dataset_symbols/dataset_shards repository (docs/06 §2·§4).

메타데이터(PostgREST)만 다룬다. shard 바이너리 업로드/검증은 batch 파이프라인이
StorageObjectClient로 수행하고, 검증이 끝난 뒤에만 record_shard를 호출한다.
"""

from __future__ import annotations

import datetime

from pivot.storage.supabase import DATASET_BUCKET, PARQUET_CONTENT_TYPE, PostgrestClient

DATASETS_TABLE = "datasets"
SYMBOLS_TABLE = "dataset_symbols"
SHARDS_TABLE = "dataset_shards"


class DatasetNotFoundError(LookupError):
    pass


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


class DatasetRepository:
    def __init__(self, db: PostgrestClient) -> None:
        self.db = db

    def create(
        self,
        *,
        name: str,
        preset_id: int,
        preset_snapshot: dict,
        timeframe: str,
        feature_columns: list[str],
        symbols: list[str],
        splits: dict[str, str],
    ) -> dict:
        """building 상태 데이터셋과 대상 종목 행(split 확정 포함)을 만든다."""
        rows = self.db.insert(
            DATASETS_TABLE,
            {
                "name": name,
                "preset_id": preset_id,
                "preset_snapshot": preset_snapshot,
                "timeframe": timeframe,
                "feature_columns": feature_columns,
                "symbol_count": len(symbols),
            },
        )
        dataset = rows[0]
        try:
            self.db.insert(
                SYMBOLS_TABLE,
                [
                    {
                        "dataset_id": dataset["id"],
                        "symbol": symbol,
                        "split": splits.get(symbol),
                    }
                    for symbol in symbols
                ],
            )
        except Exception:
            # job 시작 전 단계이므로 parent를 보상 삭제해 building orphan을 막는다.
            try:
                self.db.delete(DATASETS_TABLE, filters={"id": f"eq.{dataset['id']}"})
            except Exception:
                pass
            raise
        return dataset

    def get(self, dataset_id: int) -> dict:
        rows = self.db.select(DATASETS_TABLE, filters={"id": f"eq.{dataset_id}"})
        if not rows:
            raise DatasetNotFoundError(f"dataset {dataset_id} not found")
        return rows[0]

    def list(self) -> list[dict]:
        return self.db.select(DATASETS_TABLE, order="created_at.desc")

    def list_symbols(self, dataset_id: int) -> list[dict]:
        return self.db.select(
            SYMBOLS_TABLE,
            filters={"dataset_id": f"eq.{dataset_id}"},
            order="symbol.asc",
        )

    def set_symbol_running(self, dataset_id: int, symbol: str) -> None:
        self._update_symbol(dataset_id, symbol, {"status": "running"})

    def set_symbol_ready(
        self,
        dataset_id: int,
        symbol: str,
        *,
        sample_count: int,
        class_counts: dict,
        length_stats: dict,
    ) -> None:
        self._update_symbol(
            dataset_id,
            symbol,
            {
                "status": "ready",
                "sample_count": sample_count,
                "class_counts": class_counts,
                "length_stats": length_stats,
                "error": None,
            },
        )

    def set_symbol_failed(self, dataset_id: int, symbol: str, error: str) -> None:
        self._update_symbol(dataset_id, symbol, {"status": "failed", "error": error})

    def record_shard(
        self,
        *,
        dataset_id: int,
        symbol: str,
        shard_index: int,
        object_path: str,
        size_bytes: int,
        row_count: int,
        sha256: str,
        feature_schema: dict,
    ) -> dict:
        """업로드/검증이 끝난 shard의 메타데이터를 기록한다."""
        rows = self.db.insert(
            SHARDS_TABLE,
            {
                "dataset_id": dataset_id,
                "symbol": symbol,
                "shard_index": shard_index,
                "bucket": DATASET_BUCKET,
                "object_path": object_path,
                "content_type": PARQUET_CONTENT_TYPE,
                "size_bytes": size_bytes,
                "row_count": row_count,
                "sha256": sha256,
                "feature_schema": feature_schema,
            },
        )
        return rows[0]

    def list_shards(self, dataset_id: int) -> list[dict]:
        return self.db.select(
            SHARDS_TABLE,
            filters={"dataset_id": f"eq.{dataset_id}"},
            order="symbol.asc,shard_index.asc",
        )

    def all_shard_paths(self) -> set[str]:
        """전체 데이터셋이 참조하는 object path 집합 (orphan 판정용)."""
        rows = self.db.select(SHARDS_TABLE, columns="object_path")
        return {row["object_path"] for row in rows}

    def finalize_ready(
        self, dataset_id: int, *, sample_count: int, class_counts: dict
    ) -> dict:
        """모든 종목이 ready일 때만 호출한다 — 집계 기록 후 ready 전환."""
        rows = self.db.update(
            DATASETS_TABLE,
            {
                "status": "ready",
                "sample_count": sample_count,
                "class_counts": class_counts,
                "failure_message": None,
                "completed_at": _now(),
            },
            filters={"id": f"eq.{dataset_id}", "status": "eq.building"},
        )
        if not rows:
            raise DatasetNotFoundError(
                f"dataset {dataset_id} is not building — cannot finalize"
            )
        return rows[0]

    def mark_failed(self, dataset_id: int, message: str) -> dict:
        rows = self.db.update(
            DATASETS_TABLE,
            {"status": "failed", "failure_message": message, "completed_at": _now()},
            filters={"id": f"eq.{dataset_id}", "status": "eq.building"},
        )
        if not rows:
            raise DatasetNotFoundError(
                f"dataset {dataset_id} is not building — cannot mark failed"
            )
        return rows[0]

    def delete(self, dataset_id: int) -> None:
        """smoke test 정리용 hard delete. Storage 객체 삭제 후에 호출해야 한다."""
        self.db.delete(DATASETS_TABLE, filters={"id": f"eq.{dataset_id}"})

    def discard_building(self, dataset_id: int) -> None:
        """job 생성 전 실패한 빈 building 메타데이터를 보상 삭제한다."""
        self.db.delete(
            DATASETS_TABLE,
            filters={"id": f"eq.{dataset_id}", "status": "eq.building"},
        )

    def _update_symbol(self, dataset_id: int, symbol: str, values: dict) -> None:
        self.db.update(
            SYMBOLS_TABLE,
            values,
            filters={"dataset_id": f"eq.{dataset_id}", "symbol": f"eq.{symbol}"},
        )
