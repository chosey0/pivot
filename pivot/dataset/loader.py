"""Supabase dataset shard를 검증해 PyTorch 학습 입력으로 제공한다."""

from __future__ import annotations

import bisect
import io
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import torch
from torch.nn.utils.rnn import pad_sequence
from torch.utils.data import Dataset

from pivot.dataset.samples import SampleAccessError, _verified_shard_bytes
from pivot.dataset.transforms import sample_standardize
from pivot.storage.datasets import DatasetRepository

VALID_SPLITS = ("train", "validation", "test")


class TrainingDatasetError(RuntimeError):
    """학습 계약을 만족하지 않는 데이터셋."""


@dataclass(frozen=True)
class TrainingSample:
    features: np.ndarray
    label: int
    symbol: str
    sample_index: int
    end_time: str


class ShardDataset(Dataset[TrainingSample]):
    """한 split의 parquet shard를 필요할 때만 읽는 Dataset."""

    def __init__(
        self,
        datasets: DatasetRepository,
        storage,
        dataset_id: int,
        split: str,
        *,
        cache_root: Path,
        diagnostics=None,
    ) -> None:
        if split not in VALID_SPLITS:
            raise ValueError(f"split must be one of {VALID_SPLITS}")
        dataset = datasets.get(dataset_id)
        if dataset["status"] != "ready":
            raise TrainingDatasetError(
                f"dataset {dataset_id} is {dataset['status']!r}; training requires ready"
            )
        if diagnostics is not None:
            report = diagnostics.latest_for_dataset(dataset_id)
            if report and report["status"] == "failed":
                raise TrainingDatasetError(
                    f"dataset {dataset_id} failed its latest diagnostic report"
                )

        symbols = {
            row["symbol"]: row
            for row in datasets.list_symbols(dataset_id)
            if row.get("split") == split
        }
        if not symbols:
            raise TrainingDatasetError(f"dataset {dataset_id} has no {split} symbols")
        not_ready = [name for name, row in symbols.items() if row["status"] != "ready"]
        if not_ready:
            raise TrainingDatasetError(
                f"dataset {dataset_id} has non-ready {split} symbols: {not_ready}"
            )

        expected_columns = list(dataset["feature_columns"])
        shards = [
            row for row in datasets.list_shards(dataset_id) if row["symbol"] in symbols
        ]
        if not shards:
            raise TrainingDatasetError(f"dataset {dataset_id} has no {split} shards")
        for shard in shards:
            if shard["feature_schema"].get("columns") != expected_columns:
                raise TrainingDatasetError(
                    f"shard {shard['symbol']}#{shard['shard_index']} feature schema mismatch"
                )

        present = {shard["symbol"] for shard in shards}
        missing = [
            symbol
            for symbol, row in symbols.items()
            if row.get("sample_count", 0) > 0 and symbol not in present
        ]
        if missing:
            raise TrainingDatasetError(
                f"dataset {dataset_id} has missing shards: {missing}"
            )

        self.dataset_id = dataset_id
        self.split = split
        self.feature_columns = expected_columns
        self._storage = storage
        self._cache_dir = cache_root / str(dataset_id)
        self._shards = shards
        self._ends: list[int] = []
        total = 0
        for shard in shards:
            total += int(shard["row_count"])
            self._ends.append(total)
        if total == 0:
            raise TrainingDatasetError(
                f"dataset {dataset_id} has an empty {split} split"
            )
        self._table_cache: OrderedDict[int, object] = OrderedDict()

    def __len__(self) -> int:
        return self._ends[-1]

    def __getitem__(self, index: int) -> TrainingSample:
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        shard_index = bisect.bisect_right(self._ends, index)
        start = 0 if shard_index == 0 else self._ends[shard_index - 1]
        shard = self._shards[shard_index]
        row = self._table(shard_index).slice(index - start, 1).to_pylist()[0]
        features = sample_standardize(np.asarray(row["features"], dtype=np.float32))
        return TrainingSample(
            features=features,
            label=int(row["label"]),
            symbol=shard["symbol"],
            sample_index=int(row["sample_index"]),
            end_time=row["end_time"].isoformat(),
        )

    def labels(self) -> list[int]:
        """sampler/class weight 계산용 라벨 목록. 피처 컬럼은 읽지 않는다."""
        labels: list[int] = []
        for index in range(len(self._shards)):
            labels.extend(self._table(index, columns=["label"])["label"].to_pylist())
        return [int(label) for label in labels]

    def verify(self) -> None:
        """학습 시작 전에 split의 모든 객체·체크섬·행 수를 검증한다."""
        for index in range(len(self._shards)):
            self._table(index, columns=["label"])

    def _table(self, index: int, columns: list[str] | None = None):
        if columns is None and index in self._table_cache:
            return self._table_cache[index]
        shard = self._shards[index]
        data = _verified_shard_bytes(self._storage, shard, self._cache_dir)
        try:
            table = pq.read_table(io.BytesIO(data), columns=columns)
        except Exception as exc:
            raise SampleAccessError(
                f"cannot read shard {shard['symbol']}#{shard['shard_index']}: {exc}"
            ) from exc
        if table.num_rows != int(shard["row_count"]):
            raise SampleAccessError(
                f"shard {shard['symbol']}#{shard['shard_index']} has {table.num_rows} rows, "
                f"metadata says {shard['row_count']}"
            )
        if columns is None:
            self._table_cache[index] = table
            self._table_cache.move_to_end(index)
            if len(self._table_cache) > 2:
                self._table_cache.popitem(last=False)
        return table


def collate_samples(samples: list[TrainingSample]) -> dict[str, object]:
    """zero padding과 유효 위치 mask를 포함한 학습 batch를 만든다."""
    if not samples:
        raise ValueError("cannot collate an empty batch")
    features, lengths, mask = collate_feature_sequences(
        [sample.features for sample in samples]
    )
    return {
        "features": features,
        "labels": torch.tensor([sample.label for sample in samples], dtype=torch.long),
        "lengths": lengths,
        "mask": mask,
        "symbols": [sample.symbol for sample in samples],
        "sample_indices": [sample.sample_index for sample in samples],
        "end_times": [sample.end_time for sample in samples],
    }


def collate_feature_sequences(
    sequences: list[np.ndarray],
) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """학습과 실시간 추론이 공유하는 zero-mask padding."""
    if not sequences:
        raise ValueError("cannot collate empty feature sequences")
    tensors = [torch.from_numpy(sequence) for sequence in sequences]
    lengths = torch.tensor([len(tensor) for tensor in tensors], dtype=torch.long)
    features = pad_sequence(tensors, batch_first=True, padding_value=0.0)
    positions = torch.arange(features.shape[1]).unsqueeze(0)
    return features, lengths, positions < lengths.unsqueeze(1)
