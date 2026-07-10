"""repository 단위 테스트 — 프리셋 버전/archive 규칙과 job 상태 전이를 고정한다."""

import pytest

from pivot.config import PRESET_SCHEMA_VERSION, PreprocessPreset
from pivot.storage.datasets import DatasetRepository
from pivot.storage.jobs import JobRepository, JobTransitionError
from pivot.storage.presets import (
    PresetConflictError,
    PresetNotFoundError,
    PresetRepository,
    validate_preset,
)

from fakes import FakeDb


def preset(name: str = "테스트 프리셋") -> PreprocessPreset:
    return PreprocessPreset(name=name)


class TestPresetRepository:
    def test_create_starts_at_version_1(self):
        repo = PresetRepository(FakeDb())
        row = repo.create(preset())
        assert row["version"] == 1
        assert row["schema_version"] == PRESET_SCHEMA_VERSION
        assert row["preset"]["fractal"]["n"] == 20

    def test_create_rejects_duplicate_name(self):
        repo = PresetRepository(FakeDb())
        repo.create(preset())
        with pytest.raises(PresetConflictError):
            repo.create(preset())

    def test_create_rejects_empty_name(self):
        repo = PresetRepository(FakeDb())
        with pytest.raises(ValueError):
            repo.create(preset(name="  "))

    def test_update_appends_new_version(self):
        repo = PresetRepository(FakeDb())
        first = repo.create(preset())
        second = repo.create_version(first["id"], preset().model_copy(update={"name": "무시됨"}))
        assert second["name"] == first["name"]  # 이름은 기존 행 기준으로 유지
        assert second["version"] == 2
        # 기존 버전 행은 그대로 남는다
        assert [row["version"] for row in repo.list()] == [2, 1]

    def test_archive_hides_from_default_list(self):
        repo = PresetRepository(FakeDb())
        row = repo.create(preset())
        repo.archive(row["id"])
        assert repo.list() == []
        assert len(repo.list(include_archived=True)) == 1
        with pytest.raises(PresetConflictError):
            repo.archive(row["id"])  # 이중 archive 금지

    def test_get_missing_raises(self):
        repo = PresetRepository(FakeDb())
        with pytest.raises(PresetNotFoundError):
            repo.get(99)

    def test_validate_rejects_wrong_schema_version(self):
        with pytest.raises(ValueError):
            validate_preset(preset().model_dump(), schema_version=PRESET_SCHEMA_VERSION + 1)

    def test_validate_rejects_unknown_fields_value(self):
        broken = preset().model_dump()
        broken["fractal"]["n"] = 1  # n >= 3 위반
        with pytest.raises(ValueError):
            validate_preset(broken, schema_version=PRESET_SCHEMA_VERSION)


class TestJobTransitions:
    def make_job(self, repo: JobRepository) -> int:
        return repo.create(kind="preprocess_batch", payload={}, total_items=2)["id"]

    def test_normal_lifecycle(self):
        repo = JobRepository(FakeDb())
        job_id = self.make_job(repo)
        assert repo.get(job_id)["status"] == "queued"
        assert repo.mark_running(job_id)["status"] == "running"
        repo.set_progress(job_id, 1)
        assert repo.get(job_id)["completed_items"] == 1
        done = repo.finish(job_id, "succeeded", result={"ok": True})
        assert done["status"] == "succeeded"
        assert done["completed_at"] is not None

    def test_cannot_finish_before_running(self):
        repo = JobRepository(FakeDb())
        job_id = self.make_job(repo)
        with pytest.raises(JobTransitionError):
            repo.finish(job_id, "succeeded")

    def test_cannot_run_twice(self):
        repo = JobRepository(FakeDb())
        job_id = self.make_job(repo)
        repo.mark_running(job_id)
        with pytest.raises(JobTransitionError):
            repo.mark_running(job_id)

    def test_cannot_finish_twice(self):
        repo = JobRepository(FakeDb())
        job_id = self.make_job(repo)
        repo.mark_running(job_id)
        repo.finish(job_id, "failed", error="boom")
        with pytest.raises(JobTransitionError):
            repo.finish(job_id, "succeeded")

    def test_cancel_allowed_from_queued(self):
        repo = JobRepository(FakeDb())
        job_id = self.make_job(repo)
        assert repo.finish(job_id, "cancelled")["status"] == "cancelled"

    def test_terminal_status_only(self):
        repo = JobRepository(FakeDb())
        job_id = self.make_job(repo)
        with pytest.raises(JobTransitionError):
            repo.finish(job_id, "running")

    def test_events_are_ordered_and_filterable(self):
        repo = JobRepository(FakeDb())
        job_id = self.make_job(repo)
        for sequence in range(3):
            repo.append_event(job_id, sequence, "tick", {"sequence": sequence})
        assert [e["sequence"] for e in repo.events_after(job_id)] == [0, 1, 2]
        assert [e["sequence"] for e in repo.events_after(job_id, 1)] == [2]


def test_dataset_create_cleans_parent_when_symbol_insert_fails():
    class FailingDb(FakeDb):
        def insert(self, table: str, rows):
            if table == "dataset_symbols":
                raise RuntimeError("symbol insert failed")
            return super().insert(table, rows)

    db = FailingDb()
    repo = DatasetRepository(db)

    with pytest.raises(RuntimeError, match="symbol insert failed"):
        repo.create(
            name="broken",
            preset_id=1,
            preset_snapshot={"preset": {}},
            timeframe="day",
            feature_columns=["Open"],
            symbols=["005930"],
            splits={"005930": "train"},
        )

    assert db.tables["datasets"] == []
