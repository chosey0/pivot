"""취소·삭제·정리 수명주기 검증 — 상태 전이, 삭제 순서, 정리 멱등성."""

import datetime

import pytest

from pivot.dataset import batch
from pivot.storage.datasets import DatasetNotFoundError, DatasetRepository
from pivot.storage.jobs import JobRepository, JobTransitionError
from pivot.storage.lifecycle import (
    DatasetDeletionBlockedError,
    DatasetDeletionFailedError,
    delete_dataset,
    run_cleanup,
)
from pivot.storage.supabase import DATASET_BUCKET

from fakes import FakeDb, FakeStorage
from test_batch import Harness

NOW = datetime.datetime(2026, 7, 12, 12, 0, tzinfo=datetime.UTC)
OLD = "2026-07-10T00:00:00+00:00"  # NOW 기준 24시간 초과
RECENT = "2026-07-12T11:30:00+00:00"


class TestCancelTransitions:
    def test_cancel_from_queued_and_running(self):
        jobs = JobRepository(FakeDb())
        queued = jobs.create(kind="preprocess_batch", payload={}, total_items=1)
        assert jobs.finish(queued["id"], "cancelled")["status"] == "cancelled"

        running = jobs.create(kind="preprocess_batch", payload={}, total_items=1)
        jobs.mark_running(running["id"])
        assert jobs.finish(running["id"], "cancelled")["status"] == "cancelled"

    def test_cancel_after_terminal_is_rejected(self):
        jobs = JobRepository(FakeDb())
        job = jobs.create(kind="preprocess_batch", payload={}, total_items=1)
        jobs.mark_running(job["id"])
        jobs.finish(job["id"], "succeeded")
        with pytest.raises(JobTransitionError):
            jobs.finish(job["id"], "cancelled")


class TestCancelDuringBatch:
    def test_cancel_between_symbols_stops_processing(self, tmp_path):
        class CancelAfterFirstSymbol(JobRepository):
            def set_progress(self, job_id: int, completed_items: int) -> None:
                super().set_progress(job_id, completed_items)
                if completed_items == 1:
                    self.finish(job_id, "cancelled")

        h = Harness(tmp_path, ["AAA", "BBB"], cached=["AAA", "BBB"])
        h.jobs = CancelAfterFirstSymbol(h.db)
        h.run()

        assert h.jobs.get(h.job["id"])["status"] == "cancelled"
        dataset = h.datasets.get(h.dataset["id"])
        assert dataset["status"] == "failed"
        assert dataset["failure_message"] == "cancelled by user"
        rows = {row["symbol"]: row for row in h.datasets.list_symbols(h.dataset["id"])}
        assert rows["AAA"]["status"] == "ready"
        assert rows["BBB"]["status"] == "pending"  # 두 번째 종목은 시작하지 않는다
        event_types = [e["event_type"] for e in h.jobs.events_after(h.job["id"])]
        assert event_types[-1] == "job_cancelled"

    def test_cancel_between_shard_uploads(self, tmp_path):
        h = Harness(tmp_path, ["AAA"], cached=["AAA"])
        h.jobs.mark_running(h.job["id"])
        storage = FakeStorage()
        with pytest.raises(batch.BatchCancelledError):
            batch._process_symbol(
                datasets=h.datasets,
                storage=storage,
                dataset_id=h.dataset["id"],
                symbol="AAA",
                preset=h.preset,
                data_root=h.data_root,
                broker="kiwoom",
                is_cancelled=lambda: True,
            )
        assert storage.objects == {}  # 취소 후에는 업로드하지 않는다

    def test_late_cancel_never_finalizes_ready(self, tmp_path):
        """마지막 종목 처리 후 도착한 취소도 ready 확정을 막는다."""

        class CancelOnLastProgress(JobRepository):
            def set_progress(self, job_id: int, completed_items: int) -> None:
                super().set_progress(job_id, completed_items)
                self.finish(job_id, "cancelled")

        h = Harness(tmp_path, ["AAA"], cached=["AAA"])
        h.jobs = CancelOnLastProgress(h.db)
        h.run()
        assert h.datasets.get(h.dataset["id"])["status"] == "failed"
        assert h.jobs.get(h.job["id"])["status"] == "cancelled"


def make_deletable_dataset(db: FakeDb, storage: FakeStorage, *, status: str = "ready"):
    datasets = DatasetRepository(db)
    dataset = datasets.create(
        name="삭제 대상",
        preset_id=1,
        preset_snapshot={},
        timeframe="day",
        feature_columns=["Close"],
        symbols=["AAA"],
        splits={"AAA": "train"},
    )
    path = f"datasets/{dataset['id']}/AAA/part-00000-{'a' * 12}.parquet"
    storage.upload(DATASET_BUCKET, path, b"shard-bytes", content_type="application/x")
    datasets.record_shard(
        dataset_id=dataset["id"],
        symbol="AAA",
        shard_index=0,
        object_path=path,
        size_bytes=11,
        row_count=1,
        sha256="a" * 64,
        feature_schema={"columns": ["Close"]},
    )
    if status == "ready":
        datasets.set_symbol_ready(
            dataset["id"], "AAA", sample_count=1, class_counts={}, length_stats={}
        )
        datasets.finalize_ready(dataset["id"], sample_count=1, class_counts={})
    elif status == "failed":
        datasets.mark_failed(dataset["id"], "테스트 실패 상태")
    return datasets, dataset["id"], path


class TestDeleteDataset:
    def test_objects_removed_before_metadata(self):
        db, storage = FakeDb(), FakeStorage()
        datasets, dataset_id, path = make_deletable_dataset(db, storage)
        jobs = JobRepository(db)

        result = delete_dataset(
            datasets=datasets, jobs=jobs, storage=storage, dataset_id=dataset_id
        )

        assert storage.removed == [[path]]  # 확정된 목록 그대로 삭제
        assert (DATASET_BUCKET, path) not in storage.objects
        with pytest.raises(DatasetNotFoundError):
            datasets.get(dataset_id)
        assert db.tables["dataset_symbols"] == []
        assert db.tables["dataset_shards"] == []

        job = jobs.get(result["job_id"])
        assert job["kind"] == "dataset_delete"
        assert job["status"] == "succeeded"
        assert job["payload"]["object_paths"] == [path]

    def test_building_dataset_is_blocked(self):
        db, storage = FakeDb(), FakeStorage()
        datasets, dataset_id, _ = make_deletable_dataset(db, storage, status="building")
        with pytest.raises(DatasetDeletionBlockedError):
            delete_dataset(
                datasets=datasets,
                jobs=JobRepository(db),
                storage=storage,
                dataset_id=dataset_id,
            )
        datasets.get(dataset_id)  # 메타데이터는 그대로

    def test_partial_failure_keeps_metadata_and_is_retryable(self):
        class FlakyStorage(FakeStorage):
            def __init__(self) -> None:
                super().__init__()
                self.fail_next_remove = True

            def remove(self, bucket: str, paths: list[str]) -> None:
                if self.fail_next_remove:
                    self.fail_next_remove = False
                    raise RuntimeError("storage unavailable")
                super().remove(bucket, paths)

        db, storage = FakeDb(), FlakyStorage()
        datasets, dataset_id, path = make_deletable_dataset(db, storage)
        jobs = JobRepository(db)

        with pytest.raises(DatasetDeletionFailedError):
            delete_dataset(
                datasets=datasets, jobs=jobs, storage=storage, dataset_id=dataset_id
            )
        # 객체 삭제가 실패했으니 메타데이터는 남아 있어야 한다 (순서 보장)
        assert datasets.get(dataset_id)["id"] == dataset_id
        assert (DATASET_BUCKET, path) in storage.objects
        failed_jobs = [row for row in db.tables["jobs"] if row["kind"] == "dataset_delete"]
        assert failed_jobs[0]["status"] == "failed"
        assert failed_jobs[0]["payload"]["object_paths"] == [path]

        # 같은 호출이 그대로 재시도가 된다
        delete_dataset(
            datasets=datasets, jobs=jobs, storage=storage, dataset_id=dataset_id
        )
        with pytest.raises(DatasetNotFoundError):
            datasets.get(dataset_id)
        assert (DATASET_BUCKET, path) not in storage.objects


class TestCleanup:
    def setup(self):
        db, storage = FakeDb(), FakeStorage()
        return db, storage, DatasetRepository(db), JobRepository(db)

    def test_stale_jobs_and_datasets_are_finalized(self):
        db, storage, datasets, jobs = self.setup()
        stale_job = jobs.create(kind="preprocess_batch", payload={}, total_items=1)
        fresh_job = jobs.create(kind="preprocess_batch", payload={}, total_items=1)
        db.update("jobs", {"created_at": OLD}, filters={"id": f"eq.{stale_job['id']}"})
        db.update("jobs", {"created_at": RECENT}, filters={"id": f"eq.{fresh_job['id']}"})

        stale_dataset = datasets.create(
            name="스테일", preset_id=1, preset_snapshot={}, timeframe="day",
            feature_columns=["Close"], symbols=["AAA"], splits={},
        )
        db.update(
            "datasets", {"created_at": OLD}, filters={"id": f"eq.{stale_dataset['id']}"}
        )

        report = run_cleanup(datasets=datasets, jobs=jobs, storage=storage, now=NOW)
        assert report["stale_jobs_cancelled"] == [stale_job["id"]]
        assert report["stale_datasets_failed"] == [stale_dataset["id"]]
        assert jobs.get(stale_job["id"])["status"] == "cancelled"
        assert jobs.get(fresh_job["id"])["status"] == "queued"
        assert datasets.get(stale_dataset["id"])["status"] == "failed"

    def test_building_dataset_with_active_job_is_protected(self):
        db, storage, datasets, jobs = self.setup()
        dataset = datasets.create(
            name="진행중", preset_id=1, preset_snapshot={}, timeframe="day",
            feature_columns=["Close"], symbols=["AAA"], splits={},
        )
        db.update("datasets", {"created_at": OLD}, filters={"id": f"eq.{dataset['id']}"})
        job = jobs.create(
            kind="preprocess_batch",
            payload={"dataset_id": dataset["id"]},
            total_items=1,
        )
        db.update("jobs", {"created_at": RECENT}, filters={"id": f"eq.{job['id']}"})

        report = run_cleanup(datasets=datasets, jobs=jobs, storage=storage, now=NOW)
        assert report["stale_datasets_failed"] == []
        assert datasets.get(dataset["id"])["status"] == "building"

    def test_orphan_objects_are_removed_conservatively(self):
        db, storage, datasets, jobs = self.setup()
        datasets_repo, dataset_id, referenced_path = make_deletable_dataset(db, storage)
        building = datasets.create(
            name="빌딩", preset_id=1, preset_snapshot={}, timeframe="day",
            feature_columns=["Close"], symbols=["AAA"], splits={},
        )
        db.update("datasets", {"created_at": RECENT}, filters={"id": f"eq.{building['id']}"})

        old_orphan = "datasets/999/AAA/part-00000-beefbeefbeef.parquet"
        young_orphan = "datasets/998/AAA/part-00000-cafecafecafe.parquet"
        building_object = f"datasets/{building['id']}/AAA/part-00000-feedfeedfeed.parquet"
        for path in (old_orphan, young_orphan, building_object):
            storage.upload(DATASET_BUCKET, path, b"x", content_type="application/x")
        storage.created_at[(DATASET_BUCKET, old_orphan)] = OLD
        storage.created_at[(DATASET_BUCKET, young_orphan)] = RECENT
        storage.created_at[(DATASET_BUCKET, building_object)] = OLD

        report = run_cleanup(datasets=datasets, jobs=jobs, storage=storage, now=NOW)
        assert report["orphan_objects_removed"] == [old_orphan]
        assert (DATASET_BUCKET, old_orphan) not in storage.objects
        assert (DATASET_BUCKET, young_orphan) in storage.objects  # age 미달 보호
        assert (DATASET_BUCKET, building_object) in storage.objects  # 진행 중 보호
        assert (DATASET_BUCKET, referenced_path) in storage.objects  # ready 참조 보호
        assert datasets_repo.get(dataset_id)["status"] == "ready"  # ready는 불변

    def test_cleanup_is_idempotent(self):
        db, storage, datasets, jobs = self.setup()
        stale_job = jobs.create(kind="preprocess_batch", payload={}, total_items=1)
        db.update("jobs", {"created_at": OLD}, filters={"id": f"eq.{stale_job['id']}"})
        orphan = "datasets/999/AAA/part-00000-beefbeefbeef.parquet"
        storage.upload(DATASET_BUCKET, orphan, b"x", content_type="application/x")
        storage.created_at[(DATASET_BUCKET, orphan)] = OLD

        first = run_cleanup(datasets=datasets, jobs=jobs, storage=storage, now=NOW)
        second = run_cleanup(datasets=datasets, jobs=jobs, storage=storage, now=NOW)
        assert first["stale_jobs_cancelled"] == [stale_job["id"]]
        assert first["orphan_objects_removed"] == [orphan]
        assert second == {
            "stale_jobs_cancelled": [],
            "stale_datasets_failed": [],
            "orphan_objects_removed": [],
        }
