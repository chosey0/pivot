import pytest

from pivot.storage.deployments import DeploymentRepository
from pivot.storage.runs import RunRepository

from fakes import FakeDb


def _deployable(db: FakeDb, *, status: str = "succeeded") -> tuple[dict, dict]:
    dataset = db.insert(
        "datasets",
        {
            "name": "live-data",
            "preset_id": 1,
            "preset_snapshot": {},
            "timeframe": "min1",
            "feature_columns": ["Close"],
            "status": "ready",
        },
    )[0]
    run = db.insert(
        "training_runs",
        {
            "name": "live-run",
            "dataset_id": dataset["id"],
            "status": status,
            "config": {},
            "dataset_snapshot": {},
        },
    )[0]
    artifact = db.insert(
        "training_artifacts",
        {
            "run_id": run["id"],
            "kind": "best_checkpoint",
            "bucket": "pivot-models",
            "object_path": "runs/1/best.pt",
            "size_bytes": 1,
            "sha256": "a" * 64,
            "metadata": {},
        },
    )[0]
    return run, artifact


def test_activation_keeps_history_and_only_one_active_row():
    db = FakeDb()
    first_run, first_artifact = _deployable(db)
    second_run, second_artifact = _deployable(db)
    repo = DeploymentRepository(db)

    first = repo.activate(run_id=first_run["id"], artifact_id=first_artifact["id"])
    second = repo.activate(
        run_id=second_run["id"], artifact_id=second_artifact["id"]
    )

    assert first["id"] != second["id"]
    assert repo.active()["id"] == second["id"]
    assert [row["active"] for row in db.tables["live_deployments"]] == [False, True]


def test_activation_rejects_non_succeeded_run_without_changing_active():
    db = FakeDb()
    good_run, good_artifact = _deployable(db)
    bad_run, bad_artifact = _deployable(db, status="failed")
    repo = DeploymentRepository(db)
    active = repo.activate(run_id=good_run["id"], artifact_id=good_artifact["id"])

    with pytest.raises(RuntimeError, match="not deployable"):
        repo.activate(run_id=bad_run["id"], artifact_id=bad_artifact["id"])

    assert repo.active()["id"] == active["id"]


def test_run_repository_only_returns_owned_best_artifact():
    db = FakeDb()
    run, artifact = _deployable(db)
    other_run, _ = _deployable(db)
    repo = RunRepository(db)

    assert repo.artifact(run["id"], artifact["id"])["id"] == artifact["id"]
    with pytest.raises(LookupError):
        repo.artifact(other_run["id"], artifact["id"])
