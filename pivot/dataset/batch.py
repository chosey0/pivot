"""일괄 전처리 파이프라인 — 프리셋을 관심종목에 적용해 Supabase 데이터셋 생성.

Lab 단건 preview와 동일한 `run_preprocess`를 종목마다 호출한다 (단일
파이프라인 원칙, docs/05). 진행 상태는 jobs/job_events에 durable하게 남기고,
shard는 업로드 후 재다운로드 해시 검증이 끝나야 메타데이터를 기록한다
(docs/06 §4). 종목 하나의 실패는 기록만 하고 다음 종목으로 진행한다.
"""

from __future__ import annotations

import hashlib
import logging
import random
import time
from pathlib import Path
from typing import Protocol

from pivot.config import PreprocessPreset
from pivot.dataset.build import Sample, run_preprocess
from pivot.dataset.shards import build_shards, feature_schema, object_path
from pivot.ingestion.cache import cache_path, load_cache
from pivot.storage.jobs import TERMINAL_STATUSES, JobRepository, JobTransitionError
from pivot.storage.supabase import DATASET_BUCKET, PARQUET_CONTENT_TYPE

SPLIT_METHOD = "seeded_shuffle_v1"
SPLIT_RATIOS = {"train": 0.7, "validation": 0.15, "test": 0.15}
DEFAULT_SPLIT_SEED = 42

logger = logging.getLogger(__name__)


class DatasetStore(Protocol):
    """DatasetRepository가 만족하는 메타데이터 계약 (테스트에서 fake로 대체)."""

    def set_symbol_running(self, dataset_id: int, symbol: str) -> None: ...
    def set_symbol_ready(
        self, dataset_id: int, symbol: str, *, sample_count: int,
        class_counts: dict, length_stats: dict,
    ) -> None: ...
    def set_symbol_failed(self, dataset_id: int, symbol: str, error: str) -> None: ...
    def record_shard(
        self, *, dataset_id: int, symbol: str, shard_index: int, object_path: str,
        size_bytes: int, row_count: int, sha256: str, feature_schema: dict,
    ) -> dict: ...
    def finalize_ready(
        self, dataset_id: int, *, sample_count: int, class_counts: dict
    ) -> dict: ...
    def mark_failed(self, dataset_id: int, message: str) -> dict: ...


class ObjectStore(Protocol):
    """StorageObjectClient가 만족하는 객체 접근 계약."""

    def upload(self, bucket: str, path: str, data: bytes, *, content_type: str) -> None: ...
    def download(self, bucket: str, path: str) -> bytes: ...


def assign_splits(
    symbols: list[str],
    *,
    seed: int = DEFAULT_SPLIT_SEED,
    ratios: dict[str, float] = SPLIT_RATIOS,
) -> dict[str, str]:
    """종목 단위 train/validation/test 결정적 배정 (백로그 A5 — 샘플 누수 방지).

    같은 종목 목록과 seed면 항상 같은 배정이 나온다. 종목 수가 적으면
    validation/test가 비어 있을 수 있다 (floor 배정, 나머지는 train).
    """
    ordered = sorted(set(symbols))
    random.Random(seed).shuffle(ordered)
    n_validation = int(len(ordered) * ratios["validation"])
    n_test = int(len(ordered) * ratios["test"])
    splits: dict[str, str] = {}
    for position, symbol in enumerate(ordered):
        if position < n_validation:
            splits[symbol] = "validation"
        elif position < n_validation + n_test:
            splits[symbol] = "test"
        else:
            splits[symbol] = "train"
    return splits


def split_config(seed: int = DEFAULT_SPLIT_SEED) -> dict:
    return {"method": SPLIT_METHOD, "seed": seed, "ratios": SPLIT_RATIOS}


def build_snapshot(preset_row: dict, split_conf: dict) -> dict:
    """datasets.preset_snapshot 봉투 — 프리셋 전체 + split 규칙 (docs/06 §2)."""
    return {
        "schema_version": preset_row["schema_version"],
        "preset_id": preset_row["id"],
        "preset_name": preset_row["name"],
        "preset_version": preset_row["version"],
        "preset": preset_row["preset"],
        "split": split_conf,
    }


def run_batch(
    *,
    jobs: JobRepository,
    datasets: DatasetStore,
    storage: ObjectStore,
    job_id: int,
    dataset_id: int,
    preset: PreprocessPreset,
    symbols: list[str],
    data_root: Path,
    broker: str,
) -> None:
    """생성 완료된 job/dataset 행을 받아 종목별 전처리→shard 업로드를 수행한다."""
    emit = _EventEmitter(jobs, job_id)
    try:
        jobs.mark_running(job_id)
    except JobTransitionError:
        return  # 시작 전에 취소됨
    except Exception as exc:
        try:
            datasets.mark_failed(dataset_id, f"batch worker failed to start: {exc}")
        except Exception:
            logger.exception("failed to persist batch startup failure")
        return

    emit("job_started", {"dataset_id": dataset_id, "symbols": symbols})
    failed: dict[str, str] = {}
    total_samples = 0
    total_class_counts: dict[str, int] = {}

    try:
        for completed, symbol in enumerate(symbols, start=1):
            datasets.set_symbol_running(dataset_id, symbol)
            emit("symbol_started", {"symbol": symbol})
            try:
                summary = _process_symbol(
                    datasets=datasets,
                    storage=storage,
                    dataset_id=dataset_id,
                    symbol=symbol,
                    preset=preset,
                    data_root=data_root,
                    broker=broker,
                )
            except Exception as exc:  # 종목 실패는 기록하고 계속 진행
                failed[symbol] = str(exc)
                datasets.set_symbol_failed(dataset_id, symbol, str(exc))
                emit("symbol_failed", {"symbol": symbol, "error": str(exc)})
            else:
                datasets.set_symbol_ready(
                    dataset_id,
                    symbol,
                    sample_count=summary["sample_count"],
                    class_counts=summary["class_counts"],
                    length_stats=summary["length_stats"],
                )
                total_samples += summary["sample_count"]
                for label, count in summary["class_counts"].items():
                    total_class_counts[label] = total_class_counts.get(label, 0) + count
                emit("symbol_succeeded", {"symbol": symbol, **summary})
            try:
                jobs.set_progress(job_id, completed)
            except Exception:
                logger.exception("failed to persist progress for job %s", job_id)

        result = {
            "dataset_id": dataset_id,
            "sample_count": total_samples,
            "class_counts": total_class_counts,
            "failed_symbols": failed,
        }
        if failed:
            message = f"{len(failed)}/{len(symbols)} symbols failed: " + ", ".join(
                sorted(failed)
            )
            datasets.mark_failed(dataset_id, message)
            emit("dataset_failed", {"message": message})
            _finish_job(
                jobs, job_id, "failed", result=result, error=message
            )
        else:
            datasets.finalize_ready(
                dataset_id, sample_count=total_samples, class_counts=total_class_counts
            )
            emit("dataset_ready", result)
            # ready는 검증된 shard의 최종 데이터 상태다. 이후 job/event 텔레메트리
            # 실패가 발생해도 데이터셋을 failed로 되돌리지 않는다.
            if not _finish_job(jobs, job_id, "succeeded", result=result):
                message = (
                    "dataset is ready, but the job success state could not be persisted"
                )
                emit(
                    "job_finalization_failed",
                    {"dataset_id": dataset_id, "message": message},
                )
                _finish_job(
                    jobs,
                    job_id,
                    "failed",
                    result=result,
                    error=message,
                )
    except Exception as exc:  # 파이프라인 자체가 죽으면 원인을 durable하게 보존
        message = f"batch pipeline crashed: {exc}"
        try:
            datasets.mark_failed(dataset_id, message)
            emit("dataset_failed", {"message": message})
        except Exception:
            logger.exception("failed to persist dataset failure for %s", dataset_id)
        _finish_job(jobs, job_id, "failed", error=message)


def _process_symbol(
    *,
    datasets: DatasetStore,
    storage: ObjectStore,
    dataset_id: int,
    symbol: str,
    preset: PreprocessPreset,
    data_root: Path,
    broker: str,
) -> dict:
    timeframe = preset.timeframe.code
    df = load_cache(cache_path(data_root, broker, timeframe, symbol))
    if df is None or df.empty:
        raise RuntimeError(f"no cached data for {symbol} ({timeframe}) — run ingest first")

    result = run_preprocess(df, preset)
    if not result.samples:
        raise RuntimeError(
            f"preprocess produced no samples for {symbol} ({timeframe}); "
            "adjust the preset or collect more bars"
        )
    shards = build_shards(result.frame, result.samples, result.feature_columns)
    schema = feature_schema(result.feature_columns)
    for shard in shards:
        path = object_path(dataset_id, symbol, shard.index, shard.sha256)
        storage.upload(DATASET_BUCKET, path, shard.data, content_type=PARQUET_CONTENT_TYPE)
        echoed = hashlib.sha256(storage.download(DATASET_BUCKET, path)).hexdigest()
        if echoed != shard.sha256:
            raise RuntimeError(
                f"shard verification failed for {path}: uploaded object hash mismatch"
            )
        datasets.record_shard(
            dataset_id=dataset_id,
            symbol=symbol,
            shard_index=shard.index,
            object_path=path,
            size_bytes=len(shard.data),
            row_count=shard.row_count,
            sha256=shard.sha256,
            feature_schema=schema,
        )

    return {
        "sample_count": len(result.samples),
        "class_counts": {
            str(label): count for label, count in result.stats["class_counts"].items()
        },
        "length_stats": _length_stats(result.samples),
        "shard_count": len(shards),
        "bars": result.stats["bars"],
        "dropped_nan": result.stats["dropped_nan"],
        "dropped_unpaired": result.stats["dropped_unpaired"],
    }


def _length_stats(samples: list[Sample]) -> dict:
    if not samples:
        return {}
    lengths = [sample.length for sample in samples]
    return {
        "min": min(lengths),
        "max": max(lengths),
        "mean": round(sum(lengths) / len(lengths), 2),
    }


class _EventEmitter:
    """job_events sequence를 0부터 단조 증가시키는 헬퍼."""

    def __init__(self, jobs: JobRepository, job_id: int) -> None:
        self._jobs = jobs
        self._job_id = job_id
        self._sequence = 0

    def __call__(self, event_type: str, payload: dict) -> None:
        try:
            self._jobs.append_event(self._job_id, self._sequence, event_type, payload)
        except Exception:
            logger.exception(
                "failed to append %s event for job %s", event_type, self._job_id
            )
        else:
            self._sequence += 1


def _finish_job(
    jobs: JobRepository,
    job_id: int,
    status: str,
    *,
    result: dict | None = None,
    error: str | None = None,
) -> bool:
    """job 종료를 재시도하고, 응답 유실 시 DB의 terminal 상태를 확인한다."""
    last_error: Exception | None = None
    for delay in (0.0, 0.05, 0.15):
        if delay:
            time.sleep(delay)
        try:
            jobs.finish(job_id, status, result=result, error=error)
            return True
        except Exception as exc:
            last_error = exc
            try:
                current = jobs.get(job_id)
            except Exception:
                current = None
            if current is not None and current["status"] in TERMINAL_STATUSES:
                return True

    logger.error(
        "failed to finalize job %s as %s after retries: %s",
        job_id,
        status,
        last_error,
    )
    return False
