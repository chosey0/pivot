from pivot.config import TrainingConfig
from pivot.storage.diagnostics import DiagnosticReportRepository
from pivot.storage.jobs import JobRepository
from pivot.storage.runs import RunRepository
from pivot.training.runs import dataset_snapshot, public_artifact, run_training

from fakes import FakeDb, FakeStorage
from test_samples import make_ready_dataset


def test_training_run_persists_epochs_evaluations_and_verified_checkpoint(tmp_path):
    db, storage = FakeDb(), FakeStorage()
    datasets, dataset_id, _ = make_ready_dataset(
        db, storage, symbols=("AAA", "BBB", "CCC")
    )
    db.tables["dataset_symbols"][0]["split"] = "train"
    db.tables["dataset_symbols"][1]["split"] = "validation"
    db.tables["dataset_symbols"][2]["split"] = "test"

    runs = RunRepository(db)
    jobs = JobRepository(db)
    config = TrainingConfig(
        model="cnn1d_temporal_v1", epochs=1, batch_size=64, sampler="none"
    )
    run = runs.create(
        name="smoke",
        dataset_id=dataset_id,
        config=config.model_dump(mode="json"),
        snapshot=dataset_snapshot(datasets, dataset_id),
    )
    job = jobs.create(kind="training", payload={"run_id": run["id"]}, total_items=1)
    runs.attach_job(run["id"], job["id"])

    run_training(
        runs=runs,
        jobs=jobs,
        datasets=datasets,
        diagnostics=DiagnosticReportRepository(db),
        storage=storage,
        run_id=run["id"],
        job_id=job["id"],
        cache_root=tmp_path,
    )

    detail = runs.detail(run["id"])
    assert detail["run"]["status"] == "succeeded"
    assert jobs.get(job["id"])["status"] == "succeeded"
    assert len(detail["epochs"]) == 1
    assert {row["metrics"]["split"] for row in detail["evaluations"]} == {
        "validation",
        "test",
    }
    artifact = detail["artifacts"][0]
    assert storage.download(artifact["bucket"], artifact["object_path"])
    assert "object_path" not in public_artifact(artifact)
    run_event = next(
        event for event in db.tables["job_events"] if event["event_type"] == "run"
    )
    assert run_event["payload"]["deployment_ids"] == []
    assert [event["event_type"] for event in db.tables["job_events"]][
        -1
    ] == "run_succeeded"


def test_training_run_rejects_checkpoint_hash_mismatch(tmp_path):
    class CorruptCheckpointStorage(FakeStorage):
        def download(self, bucket, path):
            data = super().download(bucket, path)
            return data + b"bad" if bucket == "pivot-models" else data

    db, storage = FakeDb(), CorruptCheckpointStorage()
    datasets, dataset_id, _ = make_ready_dataset(
        db, storage, symbols=("AAA", "BBB", "CCC")
    )
    for row, split in zip(
        db.tables["dataset_symbols"], ("train", "validation", "test"), strict=True
    ):
        row["split"] = split
    runs, jobs = RunRepository(db), JobRepository(db)
    config = TrainingConfig(epochs=1, batch_size=64, sampler="none")
    run = runs.create(
        name="bad-checkpoint",
        dataset_id=dataset_id,
        config=config.model_dump(mode="json"),
        snapshot=dataset_snapshot(datasets, dataset_id),
    )
    job = jobs.create(kind="training", payload={}, total_items=1)
    runs.attach_job(run["id"], job["id"])

    run_training(
        runs=runs,
        jobs=jobs,
        datasets=datasets,
        diagnostics=DiagnosticReportRepository(db),
        storage=storage,
        run_id=run["id"],
        job_id=job["id"],
        cache_root=tmp_path,
    )

    assert runs.get(run["id"])["status"] == "failed"
    assert runs.detail(run["id"])["artifacts"] == []


def test_cancel_after_epoch_prevents_evaluation_and_artifact(tmp_path):
    class CancelAfterEpochJobs(JobRepository):
        def append_event(self, job_id, sequence, event_type, payload):
            row = super().append_event(job_id, sequence, event_type, payload)
            if event_type == "epoch":
                self.finish(job_id, "cancelled", error="cancelled by test")
            return row

    db, storage = FakeDb(), FakeStorage()
    datasets, dataset_id, _ = make_ready_dataset(
        db, storage, symbols=("AAA", "BBB", "CCC")
    )
    for row, split in zip(
        db.tables["dataset_symbols"], ("train", "validation", "test"), strict=True
    ):
        row["split"] = split
    runs, jobs = RunRepository(db), CancelAfterEpochJobs(db)
    config = TrainingConfig(epochs=1, batch_size=64, sampler="none")
    run = runs.create(
        name="cancel-boundary",
        dataset_id=dataset_id,
        config=config.model_dump(mode="json"),
        snapshot=dataset_snapshot(datasets, dataset_id),
    )
    job = jobs.create(kind="training", payload={}, total_items=1)
    runs.attach_job(run["id"], job["id"])

    run_training(
        runs=runs,
        jobs=jobs,
        datasets=datasets,
        diagnostics=DiagnosticReportRepository(db),
        storage=storage,
        run_id=run["id"],
        job_id=job["id"],
        cache_root=tmp_path,
    )

    detail = runs.detail(run["id"])
    assert detail["run"]["status"] == "cancelled"
    assert detail["evaluations"] == []
    assert detail["artifacts"] == []
