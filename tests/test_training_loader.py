"""M4 학습 입력 계약: split, 표준화, shard 무결성, zero-mask padding."""

import numpy as np
import pytest
import torch

from pivot.dataset.loader import ShardDataset, TrainingDatasetError, collate_samples
from pivot.dataset.transforms import sample_standardize
from pivot.storage.diagnostics import DiagnosticReportRepository

from fakes import FakeDb, FakeStorage
from test_samples import make_ready_dataset


def test_sample_standardize_handles_constant_features_without_mutation():
    raw = np.array([[1.0, 7.0], [3.0, 7.0], [5.0, 7.0]], dtype=np.float64)
    original = raw.copy()
    result = sample_standardize(raw)

    np.testing.assert_array_equal(raw, original)
    np.testing.assert_allclose(result.mean(axis=0), [0.0, 0.0], atol=1e-6)
    np.testing.assert_allclose(result[:, 1], 0.0)
    assert result.dtype == np.float32


def test_loader_uses_stored_symbol_split_and_verified_shards(tmp_path):
    db, storage = FakeDb(), FakeStorage()
    datasets, dataset_id, expected = make_ready_dataset(db, storage)
    db.tables["dataset_symbols"][1]["split"] = "validation"

    train = ShardDataset(datasets, storage, dataset_id, "train", cache_root=tmp_path)
    validation = ShardDataset(
        datasets, storage, dataset_id, "validation", cache_root=tmp_path
    )

    assert len(train) == len(expected["AAA"].samples)
    assert len(validation) == len(expected["BBB"].samples)
    assert train[0].symbol == "AAA"
    assert validation[0].symbol == "BBB"
    np.testing.assert_allclose(train[0].features.mean(axis=0), 0.0, atol=1e-5)


def test_loader_uses_sample_level_stratified_split(tmp_path):
    db, storage = FakeDb(), FakeStorage()
    datasets, dataset_id, expected = make_ready_dataset(
        db, storage, sample_level=True
    )

    splits = {
        name: ShardDataset(datasets, storage, dataset_id, name, cache_root=tmp_path)
        for name in ("train", "validation", "test")
    }

    assert sum(map(len, splits.values())) == sum(
        len(result.samples) for result in expected.values()
    )
    assert all(dataset[0].symbol in expected for dataset in splits.values())


def test_loader_rejects_latest_failed_diagnostic(tmp_path):
    db, storage = FakeDb(), FakeStorage()
    datasets, dataset_id, _ = make_ready_dataset(db, storage)
    diagnostics = DiagnosticReportRepository(db)
    diagnostics.create(
        target_type="dataset",
        dataset_id=dataset_id,
        status="failed",
        summary={},
        report={},
    )

    with pytest.raises(TrainingDatasetError, match="failed its latest diagnostic"):
        ShardDataset(
            datasets,
            storage,
            dataset_id,
            "train",
            cache_root=tmp_path,
            diagnostics=diagnostics,
        )


def test_loader_rejects_corrupt_shard_on_access(tmp_path):
    db, storage = FakeDb(), FakeStorage()
    datasets, dataset_id, _ = make_ready_dataset(db, storage)
    storage.corrupt_paths.add(next(iter(storage.objects))[1])
    dataset = ShardDataset(datasets, storage, dataset_id, "train", cache_root=tmp_path)

    with pytest.raises(Exception, match="checksum mismatch"):
        dataset[0]


def test_collate_zero_pads_and_marks_only_valid_bars(tmp_path):
    db, storage = FakeDb(), FakeStorage()
    datasets, dataset_id, _ = make_ready_dataset(db, storage)
    dataset = ShardDataset(datasets, storage, dataset_id, "train", cache_root=tmp_path)
    short, long = sorted(
        (dataset[0], dataset[1]), key=lambda sample: len(sample.features)
    )
    batch = collate_samples([short, long])

    assert batch["features"].shape[0] == 2
    assert batch["mask"].dtype == torch.bool
    assert batch["mask"][0].sum().item() == len(short.features)
    assert batch["mask"][1].sum().item() == len(long.features)
    assert (batch["features"][0, len(short.features) :] == 0).all()
