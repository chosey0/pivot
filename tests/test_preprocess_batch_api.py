"""batch 시작 단계의 보상 정리가 building orphan을 남기지 않는지 검증한다."""

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from pivot.config import FractalConfig, LabelingConfig, PreprocessPreset
from pivot.storage.datasets import DatasetRepository
from pivot.storage.jobs import JobRepository
from server.routers import preprocess

from fakes import FakeDb, make_candles


def test_preview_markers_reference_incoming_sample_by_index(monkeypatch):
    monkeypatch.setattr(preprocess, "load_cache", lambda path: make_candles(length=120))
    preset = PreprocessPreset(
        fractal=FractalConfig(n=5),
        features=["Open", "High", "Low", "Close"],
        labeling=LabelingConfig(ignore_rule="none"),
    )

    response = preprocess.preview(
        preprocess.PreviewRequest(symbol="005930", params=preset)
    )

    included = [
        marker for marker in response["markers"] if marker["incoming_sample_included"]
    ]
    assert included
    for marker in included:
        sample = response["samples"][marker["incoming_sample_index"]]
        assert sample["end_position"] == marker["position"]
        assert sample["label"] == marker["incoming_sample_label"]


def test_batch_request_rejects_path_like_symbol():
    with pytest.raises(ValidationError, match="invalid domestic symbol"):
        preprocess.BatchRequest(
            preset_id=1,
            dataset_name="invalid-symbol",
            symbols=["../../outside"],
        )


def test_preview_uses_overseas_cache_path(monkeypatch):
    paths = []
    monkeypatch.setattr(
        preprocess,
        "load_cache",
        lambda path: paths.append(path) or make_candles(length=120),
    )
    preset = PreprocessPreset(
        fractal=FractalConfig(n=5),
        features=["Open", "High", "Low", "Close"],
        labeling=LabelingConfig(ignore_rule="none"),
    )

    response = preprocess.preview(
        preprocess.PreviewRequest(
            symbol="AAPL",
            region="overseas",
            exchange="ND",
            params=preset,
        )
    )

    assert paths[0].as_posix().endswith("raw/kiwoom/overseas/ND/AAPL/day")
    assert response["candles"][0]["time"] == "2025-01-02"
    assert response["markers"][0]["time"] >= "2025-01-02"


def test_batch_request_accepts_overseas_source():
    request = preprocess.BatchRequest(
        preset_id=1,
        dataset_name="us-stocks",
        symbols=["aapl"],
        sources={
            "AAPL": preprocess.InstrumentSource(region="overseas", exchange="nd")
        },
    )

    assert request.symbols == ["AAPL"]
    assert request.sources["AAPL"].exchange == "ND"


def test_batch_request_allows_same_symbol_with_distinct_collection_targets():
    request = preprocess.BatchRequest(
        preset_id=1,
        dataset_name="mixed",
        symbols=[],
        targets=[
            preprocess.BatchTarget(symbol="005930", timeframe="day"),
            preprocess.BatchTarget(symbol="005930", timeframe="min1"),
        ],
    )

    assert [target.timeframe for target in request.targets] == ["day", "min1"]


def test_batch_request_rejects_exact_duplicate_collection_target():
    with pytest.raises(ValidationError, match="duplicate batch targets"):
        preprocess.BatchRequest(
            preset_id=1,
            dataset_name="duplicate",
            symbols=[],
            targets=[
                preprocess.BatchTarget(symbol="005930", timeframe="day"),
                preprocess.BatchTarget(symbol="005930", timeframe="day"),
            ],
        )


def test_legacy_batch_request_rejects_duplicate_symbols():
    with pytest.raises(ValidationError, match="duplicate batch targets"):
        preprocess.BatchRequest(
            preset_id=1,
            dataset_name="duplicate-legacy",
            symbols=["005930", "005930"],
        )


def test_batch_start_records_mixed_targets_and_counts_each_collection_item(monkeypatch):
    db = FakeDb()
    datasets = DatasetRepository(db)
    jobs = JobRepository(db)
    preset = PreprocessPreset(name="mixed-batch")
    preset_row = {
        "id": 1,
        "name": preset.name,
        "version": 1,
        "schema_version": 1,
        "preset": preset.model_dump(mode="json"),
        "archived_at": None,
    }

    class Presets:
        def get(self, preset_id: int) -> dict:
            return preset_row

    started = []
    monkeypatch.setattr(preprocess, "preset_repo", lambda: Presets())
    monkeypatch.setattr(preprocess, "dataset_repo", lambda: datasets)
    monkeypatch.setattr(preprocess, "job_repo", lambda: jobs)
    monkeypatch.setattr(preprocess, "object_storage", lambda: object())
    monkeypatch.setattr(preprocess, "start_background", started.append)

    response = preprocess.start_batch(
        preprocess.BatchRequest(
            preset_id=1,
            dataset_name="mixed-targets",
            symbols=[],
            targets=[
                preprocess.BatchTarget(symbol="005930", timeframe="day"),
                preprocess.BatchTarget(symbol="005930", timeframe="min1"),
            ],
        )
    )

    dataset = datasets.get(response["dataset_id"])
    job = jobs.get(response["job_id"])
    assert dataset["timeframe"] == "mixed"
    assert [row["timeframe"] for row in dataset["preset_snapshot"]["targets"]] == [
        "day",
        "min1",
    ]
    assert dataset["symbol_count"] == 1
    assert job["total_items"] == 2
    assert len(started) == 1


def test_job_creation_failure_discards_building_dataset(monkeypatch):
    db = FakeDb()
    datasets = DatasetRepository(db)
    preset = PreprocessPreset(name="batch-api")
    preset_row = {
        "id": 1,
        "name": preset.name,
        "version": 1,
        "schema_version": 1,
        "preset": preset.model_dump(mode="json"),
        "archived_at": None,
    }

    class Presets:
        def get(self, preset_id: int) -> dict:
            return preset_row

    class FailingJobs:
        def create(self, **kwargs):
            raise RuntimeError("job insert failed")

    monkeypatch.setattr(preprocess, "preset_repo", lambda: Presets())
    monkeypatch.setattr(preprocess, "dataset_repo", lambda: datasets)
    monkeypatch.setattr(preprocess, "job_repo", lambda: FailingJobs())

    with pytest.raises(HTTPException) as raised:
        preprocess.start_batch(
            preprocess.BatchRequest(
                preset_id=1,
                dataset_name="orphan-check",
                symbols=["005930"],
            )
        )

    assert raised.value.status_code == 503
    assert db.tables["datasets"] == []
    assert db.tables["dataset_symbols"] == []
