"""샘플 목록 → parquet shard 직렬화 (docs/06 §3).

행 = 샘플 1개. 가변 길이 시퀀스는 `features` 컬럼에
list<list<float64>>(봉 × 피처, 피처 순서는 feature_schema.columns)로 담는다.
shard는 bucket 상한(50 MiB)보다 훨씬 작게 나누고, SHA-256을 파일명과
메타데이터에 함께 기록해 무결성을 검증한다.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from pivot.dataset.build import Sample

SHARD_TARGET_BYTES = 24 * 2**20  # 압축 전 추정 기준 목표 크기
SHARD_MAX_BYTES = 50 * 2**20  # bucket file_size_limit
SHA256_PREFIX_LEN = 12

SHARD_SCHEMA = pa.schema(
    [
        ("sample_index", pa.int32()),
        ("label", pa.int8()),
        ("split", pa.string()),
        ("kind", pa.string()),
        ("start_time", pa.timestamp("us")),
        ("end_time", pa.timestamp("us")),
        ("start_position", pa.int32()),
        ("end_position", pa.int32()),
        ("length", pa.int32()),
        ("features", pa.list_(pa.list_(pa.float64()))),
    ]
)


@dataclass(frozen=True)
class ShardBlob:
    """업로드 준비가 끝난 shard 하나 (직렬화 완료 + 해시 계산됨)."""

    index: int
    data: bytes
    sha256: str
    row_count: int


def feature_schema(feature_columns: list[str]) -> dict:
    """dataset_shards.feature_schema에 기록할 시퀀스 레이아웃 계약."""
    return {
        "columns": list(feature_columns),
        "dtype": "float64",
        "layout": "features[i] = 봉 i의 값 목록 (columns 순서)",
    }


def object_path(dataset_id: int, symbol: str, shard_index: int, sha256: str) -> str:
    """immutable object path (docs/06 §3). 같은 경로는 절대 덮어쓰지 않는다."""
    prefix = sha256[:SHA256_PREFIX_LEN]
    return f"datasets/{dataset_id}/{symbol}/part-{shard_index:05d}-{prefix}.parquet"


def build_shards(
    frame: pd.DataFrame,
    samples: list[Sample],
    feature_columns: list[str],
    *,
    sample_splits: list[str] | None = None,
    target_bytes: int = SHARD_TARGET_BYTES,
) -> list[ShardBlob]:
    """run_preprocess 결과를 shard 목록으로 직렬화한다.

    sample_index는 종목 내 전역 순번이라 shard가 나뉘어도 이어진다.
    """
    if sample_splits is not None and len(sample_splits) != len(samples):
        raise ValueError("sample_splits length must match samples")
    features = frame[feature_columns].astype("float64")
    times = frame.index

    chunks: list[list[tuple[int, Sample]]] = [[]]
    estimated = 0
    for index, sample in enumerate(samples):
        sample_bytes = sample.length * len(feature_columns) * 8
        if chunks[-1] and estimated + sample_bytes > target_bytes:
            chunks.append([])
            estimated = 0
        chunks[-1].append((index, sample))
        estimated += sample_bytes

    shards: list[ShardBlob] = []
    for shard_index, chunk in enumerate(chunks):
        if not chunk:
            continue  # 샘플 0개면 shard를 만들지 않는다
        data = _serialize_chunk(chunk, features, times, sample_splits)
        if len(data) >= SHARD_MAX_BYTES:
            raise ValueError(
                f"shard {shard_index} is {len(data)} bytes — exceeds the "
                f"{SHARD_MAX_BYTES} bucket limit; lower target_bytes"
            )
        shards.append(
            ShardBlob(
                index=shard_index,
                data=data,
                sha256=hashlib.sha256(data).hexdigest(),
                row_count=len(chunk),
            )
        )
    return shards


def read_shard(data: bytes) -> pa.Table:
    """shard bytes를 다시 테이블로 읽는다 (검증/로더/테스트용)."""
    return pq.read_table(io.BytesIO(data))


def _serialize_chunk(
    chunk: list[tuple[int, Sample]],
    features: pd.DataFrame,
    times: pd.Index,
    sample_splits: list[str] | None,
) -> bytes:
    rows: dict[str, list] = {name: [] for name in SHARD_SCHEMA.names}
    for index, sample in chunk:
        window = features.iloc[sample.start_position : sample.end_position + 1]
        rows["sample_index"].append(index)
        rows["label"].append(sample.label)
        rows["split"].append(sample_splits[index] if sample_splits else None)
        rows["kind"].append(sample.kind)
        rows["start_time"].append(times[sample.start_position].to_pydatetime())
        rows["end_time"].append(times[sample.end_position].to_pydatetime())
        rows["start_position"].append(sample.start_position)
        rows["end_position"].append(sample.end_position)
        rows["length"].append(sample.length)
        rows["features"].append(window.to_numpy().tolist())

    table = pa.Table.from_pydict(rows, schema=SHARD_SCHEMA)
    buffer = io.BytesIO()
    pq.write_table(table, buffer)
    return buffer.getvalue()
