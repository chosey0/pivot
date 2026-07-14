"""데이터셋 샘플 브라우저 접근 — 필요한 shard만 내려받아 조회한다 (docs/06 §5).

전역 샘플 인덱스는 shard 정렬 순서(symbol asc, shard_index asc)와 shard 안의
행 순서로 정해지는 안정 순번이다. 목록/필터용 인덱스는 `features` 컬럼을 제외한
구조적 parquet 읽기로 만들고, 단건 상세만 해당 shard 하나를 전체로 읽는다.
데이터셋은 ready 이후 불변이므로 인덱스는 프로세스 메모리에 캐시하고,
내려받은 shard는 재생성 가능한 로컬 캐시(data/tmp)에 SHA-256 이름으로 둔다.
"""

from __future__ import annotations

import bisect
import hashlib
import io
import shutil
from pathlib import Path

import pyarrow.parquet as pq

from pivot.dataset.overlap import analyze_overlap_clusters
from pivot.dataset.batch import assign_sample_splits
from pivot.storage.datasets import DatasetRepository

# ponytail: mtime 기준 파일 수 상한 — 부족해지면 바이트 상한 LRU로 교체
DISK_CACHE_MAX_FILES = 64

_META_COLUMNS = ["sample_index", "label", "kind", "start_time", "end_time", "length"]
_POSITION_COLUMNS = ["start_position", "end_position"]
_index_cache: dict[int, dict] = {}


class DatasetNotReadyError(RuntimeError):
    """ready가 아닌 데이터셋은 샘플 조회를 거부한다 (docs/06 §5)."""


class SampleAccessError(RuntimeError):
    """shard 객체 누락/손상/행 수 불일치 등 명시적으로 드러내야 하는 오류."""


class SampleNotFoundError(LookupError):
    pass


def list_samples(
    datasets: DatasetRepository,
    storage,
    dataset_id: int,
    *,
    cache_root: Path,
    label: int | None = None,
    offset: int = 0,
    limit: int = 50,
) -> dict:
    """라벨 필터 + 페이지 단위 샘플 목록. index는 전역 안정 순번이다."""
    index = _load_index(datasets, storage, dataset_id, cache_root)
    entries = index["entries"]
    if label is not None:
        entries = [entry for entry in entries if entry["label"] == label]
    return {
        "dataset_id": dataset_id,
        "total": len(entries),
        "offset": offset,
        "limit": limit,
        "label": label,
        "items": entries[offset : offset + limit],
    }


def get_sample(
    datasets: DatasetRepository,
    storage,
    dataset_id: int,
    sample_index: int,
    *,
    cache_root: Path,
) -> dict:
    """전역 순번 하나의 상세 — 원본 피처 시퀀스를 포함한다."""
    index = _load_index(datasets, storage, dataset_id, cache_root)
    entries = index["entries"]
    if not 0 <= sample_index < len(entries):
        raise SampleNotFoundError(
            f"sample {sample_index} not found (dataset has {len(entries)} samples)"
        )
    shard_position = bisect.bisect_right(index["offsets"], sample_index) - 1
    shard = index["shards"][shard_position]
    row_in_shard = sample_index - index["offsets"][shard_position]
    data = _verified_shard_bytes(storage, shard, cache_root / str(dataset_id))
    table = pq.read_table(io.BytesIO(data))
    row = table.slice(row_in_shard, 1).to_pylist()[0]
    return {
        **entries[sample_index],
        "feature_columns": shard["feature_schema"]["columns"],
        "features": row["features"],
    }


def overlap_stats_by_symbol(
    datasets: DatasetRepository,
    storage,
    dataset_id: int,
    *,
    cache_root: Path,
    max_end_gap: int,
    max_end_gap_by_source: dict[str, int] | None = None,
) -> dict[str, dict]:
    """기존 dataset shard의 메타데이터만 읽어 종목별 overlap cluster를 계산한다."""
    entries = _load_index(datasets, storage, dataset_id, cache_root)["entries"]
    by_symbol: dict[str, list[dict]] = {}
    for entry in entries:
        by_symbol.setdefault(entry.get("source_key") or entry["symbol"], []).append(entry)
    result = {}
    for symbol, rows in by_symbol.items():
        exact = all(row.get("start_position") is not None for row in rows)
        source_gap = (max_end_gap_by_source or {}).get(symbol, max_end_gap)
        result[symbol] = {
            **analyze_overlap_clusters(rows, max_end_gap=source_gap),
            "approximate": not exact,
        }
    return result


def sample_split_stats(
    datasets: DatasetRepository,
    storage,
    dataset_id: int,
    *,
    cache_root: Path,
    seed: int,
) -> dict:
    entries = _load_index(datasets, storage, dataset_id, cache_root)["entries"]
    expected = assign_sample_splits(
        [
            (row.get("source_key") or row["symbol"], row["sample_index"], row["label"])
            for row in entries
        ],
        seed=seed,
    )
    counts: dict[str, dict[str, int]] = {}
    mismatched: list[int] = []
    for row in entries:
        label = str(row["label"])
        split = row.get("split")
        label_counts = counts.setdefault(label, {})
        label_counts[str(split)] = label_counts.get(str(split), 0) + 1
        key = row.get("source_key") or row["symbol"]
        if split != expected[(key, row["sample_index"])]:
            mismatched.append(row["index"])
    return {"counts": counts, "mismatched": mismatched, "total": len(entries)}


def evict(dataset_id: int, *, cache_root: Path | None = None) -> None:
    """데이터셋 삭제 후 메모리 인덱스와 로컬 shard 캐시를 정리한다."""
    _index_cache.pop(dataset_id, None)
    if cache_root is not None:
        shutil.rmtree(cache_root / str(dataset_id), ignore_errors=True)


def _load_index(
    datasets: DatasetRepository, storage, dataset_id: int, cache_root: Path
) -> dict:
    if dataset_id in _index_cache:
        return _index_cache[dataset_id]

    dataset = datasets.get(dataset_id)
    if dataset["status"] != "ready":
        raise DatasetNotReadyError(
            f"dataset {dataset_id} is {dataset['status']!r} — samples require a ready dataset"
        )
    splits = {row["symbol"]: row["split"] for row in datasets.list_symbols(dataset_id)}
    shards = datasets.list_shards(dataset_id)  # symbol.asc, shard_index.asc — 전역 순번 기준
    cache_dir = cache_root / str(dataset_id)

    entries: list[dict] = []
    offsets: list[int] = []
    for shard in shards:
        offsets.append(len(entries))
        data = _verified_shard_bytes(storage, shard, cache_dir)
        parquet = pq.ParquetFile(io.BytesIO(data))
        available = set(parquet.schema_arrow.names)
        columns = [
            *_META_COLUMNS,
            *(
                name
                for name in ("split", "source_key", "timeframe", *_POSITION_COLUMNS)
                if name in available
            ),
        ]
        table = parquet.read(columns=columns)  # features 제외
        if table.num_rows != shard["row_count"]:
            raise SampleAccessError(
                f"shard {shard['symbol']}#{shard['shard_index']} has {table.num_rows} rows, "
                f"metadata says {shard['row_count']}"
            )
        for row in table.to_pylist():
            entries.append(
                {
                    "index": len(entries),
                    "sample_index": row["sample_index"],
                    "symbol": shard["symbol"],
                    "source_key": row.get("source_key"),
                    "timeframe": row.get("timeframe") or dataset["timeframe"],
                    "split": row.get("split") or splits.get(shard["symbol"]),
                    "label": row["label"],
                    "kind": row["kind"],
                    "start_time": row["start_time"].isoformat(),
                    "end_time": row["end_time"].isoformat(),
                    "start_position": row.get("start_position"),
                    "end_position": row.get("end_position"),
                    "length": row["length"],
                }
            )

    index = {"shards": shards, "offsets": offsets, "entries": entries}
    _index_cache[dataset_id] = index
    return index


def _verified_shard_bytes(storage, shard: dict, cache_dir: Path) -> bytes:
    """SHA-256이 메타데이터와 일치하는 shard 바이트를 반환한다.

    로컬 캐시는 재생성 가능해야 하므로 캐시 히트도 해시를 다시 검증하고,
    불일치하면 버리고 새로 받는다 (docs/06 §5).
    """
    cached = cache_dir / f"{shard['sha256']}.parquet"
    if cached.exists():
        data = cached.read_bytes()
        if hashlib.sha256(data).hexdigest() == shard["sha256"]:
            return data
        cached.unlink(missing_ok=True)

    try:
        data = storage.download(shard["bucket"], shard["object_path"])
    except Exception as exc:
        raise SampleAccessError(
            f"shard {shard['symbol']}#{shard['shard_index']} object is missing or unreadable: {exc}"
        ) from exc
    if hashlib.sha256(data).hexdigest() != shard["sha256"]:
        raise SampleAccessError(
            f"shard {shard['symbol']}#{shard['shard_index']} checksum mismatch — "
            "object does not match dataset metadata"
        )

    cache_dir.mkdir(parents=True, exist_ok=True)
    temporary = cached.with_suffix(".tmp")
    temporary.write_bytes(data)
    temporary.replace(cached)
    _prune_cache(cache_dir)
    return data


def _prune_cache(cache_dir: Path) -> None:
    files = sorted(cache_dir.glob("*.parquet"), key=lambda path: path.stat().st_mtime)
    for path in files[: max(len(files) - DISK_CACHE_MAX_FILES, 0)]:
        path.unlink(missing_ok=True)
