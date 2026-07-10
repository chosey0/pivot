"""batch 시작 단계의 보상 정리가 building orphan을 남기지 않는지 검증한다."""

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from pivot.config import PreprocessPreset
from pivot.storage.datasets import DatasetRepository
from server.routers import preprocess

from fakes import FakeDb


def test_batch_request_rejects_path_like_symbol():
    with pytest.raises(ValidationError, match="6-digit domestic stock codes"):
        preprocess.BatchRequest(
            preset_id=1,
            dataset_name="invalid-symbol",
            symbols=["../../outside"],
        )


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
