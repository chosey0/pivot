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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Protocol

import pandas as pd

from pivot.config import PreprocessPreset, Timeframe
from pivot.dataset.build import PreprocessResult, Sample, run_preprocess
from pivot.dataset.shards import build_shards, feature_schema, object_path
from pivot.ingestion.cache import cache_path, load_cache
from pivot.ingestion.fetch import filter_overseas_day_market
from pivot.storage.jobs import TERMINAL_STATUSES, JobRepository, JobTransitionError
from pivot.storage.supabase import DATASET_BUCKET, PARQUET_CONTENT_TYPE

SPLIT_METHOD = "stratified_sample_v1"
SYMBOL_SPLIT_METHOD = "seeded_shuffle_v2"
LEGACY_SPLIT_METHOD = "seeded_shuffle_v1"
SPLIT_RATIOS = {"train": 0.6, "validation": 0.2, "test": 0.2}
SYMBOL_SPLIT_RATIOS = {"train": 0.7, "validation": 0.15, "test": 0.15}
DEFAULT_SPLIT_SEED = 42
PREPROCESS_MAX_WORKERS = 2

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
    targets: list[dict] | None = None,
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
        "targets": targets or [],
    }


def target_key(target: dict) -> str:
    """수집 항목의 재현 가능한 식별자."""
    return "|".join(
        str(target.get(field) or "")
        for field in ("region", "exchange", "symbol", "timeframe", "start", "end")
    )


def run_batch(
    *,
    jobs: JobRepository,
    datasets: DatasetStore,
    storage: ObjectStore,
    job_id: int,
    dataset_id: int,
    preset: PreprocessPreset,
    symbols: list[str],
    targets: list[dict] | None = None,
    data_root: Path,
    broker: str,
    brokers: dict[str, str] | None = None,
    split_seed: int = DEFAULT_SPLIT_SEED,
) -> None:
    """생성 완료된 job/dataset 행을 받아 수집 항목별 전처리→shard 업로드를 수행한다."""
    targets = targets or [
        {
            "symbol": symbol,
            "timeframe": preset.timeframe.code,
            "region": "domestic",
            "exchange": "",
            "broker": (brokers or {}).get(symbol, broker),
            "start": None,
            "end": None,
            "cache_start": None,
            "cache_end": None,
        }
        for symbol in symbols
    ]
    symbols = list(dict.fromkeys(target["symbol"] for target in targets))
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

    emit("job_started", {"dataset_id": dataset_id, "targets": targets})
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
        processed: dict[str, PreprocessResult] = {}
        sample_records: list[tuple[str, int, int]] = []
        completed = 0
        # 결과 DataFrame은 shard 직렬화에도 쓰므로 thread 간 전송 비용 없이 이 작업 안에
        # 보관한다. 저장소 변경은 이후 주 스레드에서만 수행한다.
        with ThreadPoolExecutor(
            max_workers=max(1, min(PREPROCESS_MAX_WORKERS, len(targets)))
        ) as executor:
            futures = {
                executor.submit(_preprocess_target, target, preset, data_root): target
                for target in targets
            }
            for future in as_completed(futures):
                if cancelled():
                    for pending in futures:
                        pending.cancel()
                    handle_cancelled()
                    return

                target = futures[future]
                symbol = target["symbol"]
                key = target_key(target)
                try:
                    processed[key] = future.result()
                except Exception as exc:  # 종목 실패는 기록하고 계속 진행
                    failed[key] = str(exc)
                    emit(
                        "symbol_failed",
                        {"target_key": key, "symbol": symbol, "error": str(exc)},
                    )
                    completed += 1
                    try:
                        jobs.set_progress(job_id, completed)
                    except Exception:
                        logger.exception("failed to persist progress for job %s", job_id)
                else:
                    emit(
                        "symbol_analyzed",
                        {
                            "target_key": key,
                            "symbol": symbol,
                            "timeframe": target["timeframe"],
                            "samples": len(processed[key].samples),
                        },
                    )

        # Future 완료 순서는 비결정적이므로 split/shard 순서는 요청한 target 순서를 유지한다.
        processable = [target for target in targets if target_key(target) in processed]
        for target in processable:
            key = target_key(target)
            sample_records.extend(
                (key, index, sample.label)
                for index, sample in enumerate(processed[key].samples)
            )

        sample_splits = assign_sample_splits(sample_records, seed=split_seed)
        summaries: dict[str, list[dict]] = {symbol: [] for symbol in symbols}
        shard_offsets: dict[str, int] = {symbol: 0 for symbol in symbols}
        started_symbols: set[str] = set()
        remaining_targets = {
            symbol: sum(target["symbol"] == symbol for target in processable)
            for symbol in symbols
        }
        for target in processable:
            if cancelled():
                handle_cancelled()
                return
            symbol = target["symbol"]
            key = target_key(target)
            if symbol not in started_symbols:
                datasets.set_symbol_running(dataset_id, symbol)
                started_symbols.add(symbol)
            emit(
                "symbol_started",
                {"target_key": key, "symbol": symbol, "timeframe": target["timeframe"]},
            )
            try:
                summary = _process_target(
                    datasets=datasets,
                    storage=storage,
                    dataset_id=dataset_id,
                    target=target,
                    preset=preset,
                    data_root=data_root,
                    preprocessed=processed[key],
                    sample_splits=sample_splits,
                    shard_index_offset=shard_offsets[symbol],
                    is_cancelled=cancelled,
                )
            except BatchCancelledError:
                datasets.set_symbol_failed(dataset_id, symbol, "cancelled by user")
                emit(
                    "symbol_failed",
                    {"target_key": key, "symbol": symbol, "error": "cancelled by user"},
                )
                handle_cancelled()
                return
            except Exception as exc:
                failed[key] = str(exc)
                emit(
                    "symbol_failed",
                    {"target_key": key, "symbol": symbol, "error": str(exc)},
                )
            else:
                summaries[symbol].append(summary)
                shard_offsets[symbol] += summary["shard_count"]
                total_samples += summary["sample_count"]
                for label, count in summary["class_counts"].items():
                    total_class_counts[label] = total_class_counts.get(label, 0) + count
                emit(
                    "symbol_succeeded",
                    {"target_key": key, "symbol": symbol, **summary},
                )
            remaining_targets[symbol] -= 1
            if remaining_targets[symbol] == 0:
                errors = [
                    message
                    for item in targets
                    if item["symbol"] == symbol
                    and (message := failed.get(target_key(item))) is not None
                ]
                if errors:
                    datasets.set_symbol_failed(dataset_id, symbol, "; ".join(errors))
                elif summaries[symbol]:
                    datasets.set_symbol_ready(
                        dataset_id,
                        symbol,
                        **_combined_symbol_summary(summaries[symbol]),
                    )
            completed += 1
            try:
                jobs.set_progress(job_id, completed)
            except Exception:
                logger.exception("failed to persist progress for job %s", job_id)

        for symbol in symbols:
            errors = [
                message
                for target in targets
                if target["symbol"] == symbol
                and (message := failed.get(target_key(target))) is not None
            ]
            if errors:
                datasets.set_symbol_failed(dataset_id, symbol, "; ".join(errors))
            elif summaries[symbol]:
                datasets.set_symbol_ready(
                    dataset_id,
                    symbol,
                    **_combined_symbol_summary(summaries[symbol]),
                )

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
            message = f"{len(failed)}/{len(targets)} targets failed: " + ", ".join(
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
    target = {
        "symbol": symbol,
        "timeframe": preset.timeframe.code,
        "region": "domestic",
        "exchange": "",
        "broker": broker,
        "start": None,
        "end": None,
        "cache_start": None,
        "cache_end": None,
    }
    return _process_target(
        datasets=datasets,
        storage=storage,
        dataset_id=dataset_id,
        target=target,
        preset=preset,
        data_root=data_root,
        sample_splits=sample_splits,
        is_cancelled=is_cancelled,
    )


def _process_target(
    *,
    datasets: DatasetStore,
    storage: ObjectStore,
    dataset_id: int,
    target: dict,
    preset: PreprocessPreset,
    data_root: Path,
    preprocessed: PreprocessResult | None = None,
    sample_splits: dict[tuple[str, int], str] | None = None,
    shard_index_offset: int = 0,
    is_cancelled: Callable[[], bool] = lambda: False,
) -> dict:
    symbol = target["symbol"]
    key = target_key(target)
    result = preprocessed if preprocessed is not None else _preprocess_target(
        target, preset, data_root
    )
    if sample_splits is None:
        sample_splits = assign_sample_splits(
            [(key, index, sample.label) for index, sample in enumerate(result.samples)]
        )
    row_splits = [sample_splits[(key, index)] for index in range(len(result.samples))]
    shards = build_shards(
        result.frame,
        result.samples,
        result.feature_columns,
        sample_splits=row_splits,
        source_key=key,
        timeframe=target["timeframe"],
    )
    schema = feature_schema(result.feature_columns)
    for shard in shards:
        if is_cancelled():
            # shard 업로드 사이의 협조적 취소 — 검증 전 업로드 잔여물은 정리 작업이 지운다
            raise BatchCancelledError(symbol)
        shard_index = shard_index_offset + shard.index
        path = object_path(dataset_id, symbol, shard_index, shard.sha256)
        storage.upload(DATASET_BUCKET, path, shard.data, content_type=PARQUET_CONTENT_TYPE)
        echoed = hashlib.sha256(storage.download(DATASET_BUCKET, path)).hexdigest()
        if echoed != shard.sha256:
            raise RuntimeError(
                f"shard verification failed for {path}: uploaded object hash mismatch"
            )
        datasets.record_shard(
            dataset_id=dataset_id,
            symbol=symbol,
            shard_index=shard_index,
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
            "dropped_short": result.stats["dropped_short"],
            "pairing_stats": result.stats["pairing_stats"],
            "overlap_clusters": result.stats["overlap_clusters"],
        },
        "shard_count": len(shards),
        "bars": result.stats["bars"],
        "dropped_nan": result.stats["dropped_nan"],
        "dropped_short": result.stats["dropped_short"],
        "dropped_unpaired": result.stats["dropped_unpaired"],
        "cleaning": result.stats["cleaning"],
        "target": {
            key: target.get(key)
            for key in ("symbol", "timeframe", "region", "exchange", "start", "end")
        },
    }


def _preprocess_symbol(
    symbol: str, preset: PreprocessPreset, data_root: Path, broker: str
):
    return _preprocess_target(
        {
            "symbol": symbol,
            "timeframe": preset.timeframe.code,
            "region": "domestic",
            "exchange": "",
            "broker": broker,
            "start": None,
            "end": None,
            "cache_start": None,
            "cache_end": None,
        },
        preset,
        data_root,
    )


def _preprocess_target(target: dict, preset: PreprocessPreset, data_root: Path):
    symbol = target["symbol"]
    timeframe = target["timeframe"]
    target_preset = preset.for_timeframe(Timeframe.from_code(timeframe))
    df = load_cache(cache_path(data_root, target["broker"], timeframe, symbol))
    if df is None or df.empty:
        raise RuntimeError(f"no cached data for {symbol} ({timeframe}) — run ingest first")
    df = filter_overseas_day_market(
        df,
        target_preset.timeframe,
        target["region"],
    )
    if target.get("cache_start"):
        df = df.loc[df.index >= pd.Timestamp(target["cache_start"])]
    if target.get("cache_end"):
        df = df.loc[df.index <= pd.Timestamp(target["cache_end"])]
    if df.empty:
        raise RuntimeError(f"no cached data in target range for {symbol} ({timeframe})")

    result = run_preprocess(df, target_preset)
    if not result.samples:
        raise RuntimeError(
            f"preprocess produced no samples for {symbol} ({timeframe}); "
            "adjust the preset or collect more bars"
        )
    return result


def _combined_symbol_summary(summaries: list[dict]) -> dict:
    if len(summaries) == 1:
        summary = summaries[0]
        return {
            "sample_count": summary["sample_count"],
            "class_counts": summary["class_counts"],
            "length_stats": {
                **summary["length_stats"],
                "cleaning": summary["cleaning"],
                "targets": [summary["target"]],
            },
        }

    counts: dict[str, int] = {}
    for summary in summaries:
        for label, count in summary["class_counts"].items():
            counts[label] = counts.get(label, 0) + count
    sample_count = sum(summary["sample_count"] for summary in summaries)
    pairing_stats = {
        "rule": summaries[0]["length_stats"]["pairing_stats"]["rule"],
        **{
            field: sum(
                int(summary["length_stats"]["pairing_stats"].get(field, 0))
                for summary in summaries
            )
            for field in (
                "adjacent_edges",
                "unpaired_markers",
                "dropped_invalid_position",
                "dropped_label2",
            )
        },
    }
    cleaning = _combined_cleaning(summaries)
    return {
        "sample_count": sample_count,
        "class_counts": counts,
        "length_stats": {
            "min": min(summary["length_stats"]["min"] for summary in summaries),
            "max": max(summary["length_stats"]["max"] for summary in summaries),
            "mean": round(
                sum(
                    summary["length_stats"]["mean"] * summary["sample_count"]
                    for summary in summaries
                )
                / sample_count,
                2,
            ),
            "targets": [summary["target"] for summary in summaries],
            "points": sum(summary["length_stats"]["points"] for summary in summaries),
            "dropped_nan": sum(
                summary["length_stats"]["dropped_nan"] for summary in summaries
            ),
            "dropped_short": sum(
                summary["length_stats"].get("dropped_short", 0) for summary in summaries
            ),
            "pairing_stats": pairing_stats,
            "overlap_clusters": _combined_overlap(summaries),
            "cleaning": cleaning,
        },
    }


def _combined_cleaning(summaries: list[dict]) -> dict:
    rows = [summary["cleaning"] for summary in summaries]
    original = sum(int(row.get("original_bars", 0)) for row in rows)
    removed = sum(int(row.get("removed_bars", 0)) for row in rows)
    reasons: dict[str, int] = {}
    for row in rows:
        for reason, count in row.get("reason_counts", {}).items():
            reasons[reason] = reasons.get(reason, 0) + int(count)
    first = rows[0]
    return {
        "mode": first.get("mode", "off"),
        "policy": first.get("policy"),
        "reference": first.get("reference"),
        "original_bars": original,
        "retained_bars": sum(int(row.get("retained_bars", 0)) for row in rows),
        "removed_bars": removed,
        "removed_ratio": removed / original if original else 0.0,
        "segments": sum(int(row.get("segments", 0)) for row in rows),
        "segment_lengths": [
            length for row in rows for length in row.get("segment_lengths", [])
        ],
        "structural_breaks": sum(
            int(row.get("structural_breaks", 0)) for row in rows
        ),
        "reason_counts": reasons,
        "thresholds": (
            first.get("thresholds", {})
            if all(row.get("thresholds", {}) == first.get("thresholds", {}) for row in rows)
            else {}
        ),
        "target_count": len(rows),
        "targets": [
            {"target": summary["target"], "stats": summary["cleaning"]}
            for summary in summaries
        ],
    }


def _combined_overlap(summaries: list[dict]) -> dict:
    rows = [summary["length_stats"]["overlap_clusters"] for summary in summaries]
    first = rows[0]
    summed = (
        "plateau_clusters",
        "plateau_clustered_points",
        "dropped_plateau_points",
        "sample_clusters",
        "clustered_samples",
        "redundant_samples",
    )
    return {
        "tie_policy": first["tie_policy"],
        **{field: sum(int(row[field]) for row in rows) for field in summed},
        "max_plateau_cluster_size": max(
            int(row["max_plateau_cluster_size"]) for row in rows
        ),
        "max_sample_cluster_size": max(
            int(row["max_sample_cluster_size"]) for row in rows
        ),
        "threshold": first["threshold"],
        "max_end_gap": first["max_end_gap"],
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
