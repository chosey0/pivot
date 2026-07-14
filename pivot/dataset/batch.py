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
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from pivot.config import PreprocessPreset
from pivot.dataset.build import Sample, run_preprocess
from pivot.dataset.shards import build_shards, feature_schema, object_path
from pivot.ingestion.cache import cache_path, load_cache
from pivot.ingestion.fetch import BROKER, filter_overseas_day_market
from pivot.storage.jobs import TERMINAL_STATUSES, JobRepository, JobTransitionError
from pivot.storage.supabase import DATASET_BUCKET, PARQUET_CONTENT_TYPE

SPLIT_METHOD = "stratified_sample_v1"
SYMBOL_SPLIT_METHOD = "seeded_shuffle_v2"
LEGACY_SPLIT_METHOD = "seeded_shuffle_v1"
SPLIT_RATIOS = {"train": 0.6, "validation": 0.2, "test": 0.2}
SYMBOL_SPLIT_RATIOS = {"train": 0.7, "validation": 0.15, "test": 0.15}
DEFAULT_SPLIT_SEED = 42

logger = logging.getLogger(__name__)


class BatchCancelledError(Exception):
    """취소 요청을 감지한 협조적 중단 신호 (종목/shard 경계에서만 확인)."""


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
    ratios: dict[str, float] = SYMBOL_SPLIT_RATIOS,
    method: str = SYMBOL_SPLIT_METHOD,
) -> dict[str, str]:
    """종목 단위 train/validation/test 결정적 배정 (백로그 A5 — 샘플 누수 방지).

    같은 종목 목록과 seed면 항상 같은 배정이 나온다. v2는 종목이 3개
    이상이면 train/validation/test를 최소 하나씩 보장한다.
    """
    ordered = sorted(set(symbols))
    random.Random(seed).shuffle(ordered)
    n_validation = int(len(ordered) * ratios["validation"])
    n_test = int(len(ordered) * ratios["test"])
    if method not in {LEGACY_SPLIT_METHOD, SYMBOL_SPLIT_METHOD}:
        raise ValueError(f"unknown split method: {method}")
    if method == SYMBOL_SPLIT_METHOD and len(ordered) >= 3:
        n_validation = max(1, n_validation)
        n_test = max(1, n_test)
    splits: dict[str, str] = {}
    for position, symbol in enumerate(ordered):
        if position < n_validation:
            splits[symbol] = "validation"
        elif position < n_validation + n_test:
            splits[symbol] = "test"
        else:
            splits[symbol] = "train"
    return splits


def assign_sample_splits(
    samples: list[tuple[str, int, int]], *, seed: int = DEFAULT_SPLIT_SEED
) -> dict[tuple[str, int], str]:
    """전체 샘플을 클래스별로 결정적 셔플해 60/20/20으로 나눈다."""
    keys = [(symbol, sample_index) for symbol, sample_index, _ in samples]
    if len(set(keys)) != len(keys):
        raise ValueError("sample keys must be unique")
    by_label: dict[int, list[tuple[str, int]]] = {}
    for symbol, sample_index, label in samples:
        by_label.setdefault(label, []).append((symbol, sample_index))

    assignments: dict[tuple[str, int], str] = {}
    split_names = ("train", "validation", "test")
    weights = (6, 2, 2)
    for label, keys in sorted(by_label.items()):
        ordered = sorted(keys)
        random.Random(seed + label).shuffle(ordered)
        counts = [len(ordered) * weight // 10 for weight in weights]
        remainders = [len(ordered) * weight % 10 for weight in weights]
        for index in sorted(
            range(len(counts)), key=lambda item: (-remainders[item], item)
        )[: len(ordered) - sum(counts)]:
            counts[index] += 1

        offset = 0
        for split, count in zip(split_names, counts, strict=True):
            for key in ordered[offset : offset + count]:
                assignments[key] = split
            offset += count
    return assignments


def split_config(seed: int = DEFAULT_SPLIT_SEED) -> dict:
    return {"method": SPLIT_METHOD, "seed": seed, "ratios": SPLIT_RATIOS}


def build_snapshot(
    preset_row: dict,
    split_conf: dict,
    *,
    preset: PreprocessPreset | None = None,
    sources: dict[str, dict] | None = None,
) -> dict:
    """datasets.preset_snapshot 봉투 — 프리셋 전체 + split 규칙 (docs/06 §2)."""
    return {
        "schema_version": preset_row["schema_version"],
        "preset_id": preset_row["id"],
        "preset_name": preset_row["name"],
        "preset_version": preset_row["version"],
        # 이전 버전 JSON에 없는 호환 기본값도 명시해 재현 가능한 스냅샷을 만든다.
        "preset": preset.model_dump(mode="json") if preset else preset_row["preset"],
        "split": split_conf,
        "sources": sources or {},
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
    brokers: dict[str, str] | None = None,
    split_seed: int = DEFAULT_SPLIT_SEED,
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

    def cancelled() -> bool:
        return _is_cancelled(jobs, job_id)

    def handle_cancelled() -> None:
        # job 행은 취소 API가 이미 terminal(cancelled)로 만들었다. 여기서는
        # building 데이터셋을 durable하게 마감하고 조용히 물러난다.
        message = "cancelled by user"
        try:
            datasets.mark_failed(dataset_id, message)
        except Exception:
            logger.exception("failed to persist cancellation for dataset %s", dataset_id)
        emit("job_cancelled", {"dataset_id": dataset_id, "message": message})

    try:
        processable: list[str] = []
        sample_records: list[tuple[str, int, int]] = []
        completed = 0
        for symbol in symbols:
            if cancelled():
                handle_cancelled()
                return
            try:
                preview = _preprocess_symbol(
                    symbol,
                    preset,
                    data_root,
                    (brokers or {}).get(symbol, broker),
                )
            except Exception as exc:  # 종목 실패는 기록하고 계속 진행
                failed[symbol] = str(exc)
                datasets.set_symbol_failed(dataset_id, symbol, str(exc))
                emit("symbol_failed", {"symbol": symbol, "error": str(exc)})
                completed += 1
                try:
                    jobs.set_progress(job_id, completed)
                except Exception:
                    logger.exception("failed to persist progress for job %s", job_id)
            else:
                processable.append(symbol)
                sample_records.extend(
                    (symbol, index, sample.label)
                    for index, sample in enumerate(preview.samples)
                )
                emit("symbol_analyzed", {"symbol": symbol, "samples": len(preview.samples)})

        sample_splits = assign_sample_splits(sample_records, seed=split_seed)
        for symbol in processable:
            if cancelled():
                handle_cancelled()
                return
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
                    broker=(brokers or {}).get(symbol, broker),
                    sample_splits=sample_splits,
                    is_cancelled=cancelled,
                )
            except BatchCancelledError:
                datasets.set_symbol_failed(dataset_id, symbol, "cancelled by user")
                emit("symbol_failed", {"symbol": symbol, "error": "cancelled by user"})
                handle_cancelled()
                return
            except Exception as exc:
                failed[symbol] = str(exc)
                datasets.set_symbol_failed(dataset_id, symbol, str(exc))
                emit("symbol_failed", {"symbol": symbol, "error": str(exc)})
            else:
                datasets.set_symbol_ready(
                    dataset_id,
                    symbol,
                    sample_count=summary["sample_count"],
                    class_counts=summary["class_counts"],
                    length_stats={
                        **summary["length_stats"],
                        "cleaning": summary["cleaning"],
                    },
                )
                total_samples += summary["sample_count"]
                for label, count in summary["class_counts"].items():
                    total_class_counts[label] = total_class_counts.get(label, 0) + count
                emit("symbol_succeeded", {"symbol": symbol, **summary})
            completed += 1
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
        if not failed and cancelled():
            # 마지막 종목 처리 중 취소가 도착했으면 ready로 확정하지 않는다
            handle_cancelled()
            return
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


def _is_cancelled(jobs: JobRepository, job_id: int) -> bool:
    try:
        job = jobs.get(job_id)
    except Exception:
        return False  # 상태 조회 실패로 파이프라인을 멈추지 않는다
    return job is not None and job["status"] == "cancelled"


def _process_symbol(
    *,
    datasets: DatasetStore,
    storage: ObjectStore,
    dataset_id: int,
    symbol: str,
    preset: PreprocessPreset,
    data_root: Path,
    broker: str,
    sample_splits: dict[tuple[str, int], str] | None = None,
    is_cancelled: Callable[[], bool] = lambda: False,
) -> dict:
    result = _preprocess_symbol(symbol, preset, data_root, broker)
    if sample_splits is None:
        sample_splits = assign_sample_splits(
            [(symbol, index, sample.label) for index, sample in enumerate(result.samples)]
        )
    row_splits = [sample_splits[(symbol, index)] for index in range(len(result.samples))]
    shards = build_shards(
        result.frame,
        result.samples,
        result.feature_columns,
        sample_splits=row_splits,
    )
    schema = feature_schema(result.feature_columns)
    for shard in shards:
        if is_cancelled():
            # shard 업로드 사이의 협조적 취소 — 검증 전 업로드 잔여물은 정리 작업이 지운다
            raise BatchCancelledError(symbol)
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
        "length_stats": {
            **_length_stats(result.samples),
            "points": result.stats["points"],
            "dropped_nan": result.stats["dropped_nan"],
            "pairing_stats": result.stats["pairing_stats"],
            "overlap_clusters": result.stats["overlap_clusters"],
        },
        "shard_count": len(shards),
        "bars": result.stats["bars"],
        "dropped_nan": result.stats["dropped_nan"],
        "dropped_unpaired": result.stats["dropped_unpaired"],
        "cleaning": result.stats["cleaning"],
    }


def _preprocess_symbol(
    symbol: str, preset: PreprocessPreset, data_root: Path, broker: str
):
    timeframe = preset.timeframe.code
    df = load_cache(cache_path(data_root, broker, timeframe, symbol))
    if df is None or df.empty:
        raise RuntimeError(f"no cached data for {symbol} ({timeframe}) — run ingest first")
    df = filter_overseas_day_market(
        df,
        preset.timeframe,
        "overseas" if broker.startswith(f"{BROKER}-overseas-") else "domestic",
    )
    if df.empty:
        raise RuntimeError(f"no non-day-market data for {symbol} ({timeframe})")

    result = run_preprocess(df, preset)
    if not result.samples:
        raise RuntimeError(
            f"preprocess produced no samples for {symbol} ({timeframe}); "
            "adjust the preset or collect more bars"
        )
    return result


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
