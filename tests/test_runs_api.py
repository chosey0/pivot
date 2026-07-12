import pytest
from fastapi import HTTPException

from pivot.config import TrainingConfig
from pivot.storage.jobs import JobRepository
from pivot.storage.runs import RunRepository
from server.routers import runs as api

from fakes import FakeDb


def repositories():
    db = FakeDb()
    dataset = db.insert(
        "datasets",
        {
            "name": "ready",
            "preset_id": 1,
            "preset_snapshot": {},
            "timeframe": "day",
            "feature_columns": ["Close"],
            "status": "ready",
        },
    )[0]
    return db, dataset, RunRepository(db), JobRepository(db)


def test_start_process_failure_marks_run_and_queued_job_failed(monkeypatch):
    _, dataset, runs, jobs = repositories()
    monkeypatch.setattr(api, "run_repo", lambda: runs)
    monkeypatch.setattr(api, "job_repo", lambda: jobs)
    monkeypatch.setattr(api, "dataset_repo", lambda: object())
    monkeypatch.setattr(api, "object_storage", lambda: object())
    monkeypatch.setattr(api, "diagnostic_repo", lambda: object())
    monkeypatch.setattr(api, "build_split_datasets", lambda *args: {})
    monkeypatch.setattr(api, "dataset_snapshot", lambda *args: {"dataset": {}})
    monkeypatch.setattr(
        api,
        "start_process",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("spawn failed")),
    )

    with pytest.raises(HTTPException) as caught:
        api.start_run(
            api.StartRunRequest(
                name="spawn-fail", dataset_id=dataset["id"], config=TrainingConfig()
            )
        )

    assert caught.value.status_code == 503
    assert runs.list()[0]["status"] == "failed"
    assert jobs.get(1)["status"] == "failed"


def test_crash_monitor_fails_queued_job(monkeypatch):
    _, dataset, runs, jobs = repositories()
    run = runs.create(name="crash", dataset_id=dataset["id"], config={}, snapshot={})
    job = jobs.create(kind="training", payload={}, total_items=1)
    runs.attach_job(run["id"], job["id"])
    monkeypatch.setattr(api, "run_repo", lambda: runs)
    monkeypatch.setattr(api, "job_repo", lambda: jobs)

    api._mark_crashed_process(run["id"], job["id"], 9)

    assert runs.get(run["id"])["status"] == "failed"
    assert jobs.get(job["id"])["status"] == "failed"


def test_stop_running_job_is_worker_cooperative(monkeypatch):
    _, dataset, runs, jobs = repositories()
    run = runs.create(name="stop", dataset_id=dataset["id"], config={}, snapshot={})
    job = jobs.create(kind="training", payload={}, total_items=1)
    runs.attach_job(run["id"], job["id"])
    jobs.mark_running(job["id"])
    runs.mark_running(run["id"], "cpu")
    monkeypatch.setattr(api, "run_repo", lambda: runs)
    monkeypatch.setattr(api, "job_repo", lambda: jobs)

    response = api.stop_run(run["id"])

    assert response == {"run_id": run["id"], "status": "cancelled"}
    assert jobs.get(job["id"])["status"] == "cancelled"
    assert runs.get(run["id"])["status"] == "running"


@pytest.mark.parametrize(
    ("value", "timeframe", "expected"),
    [
        ("2017-07-19T00:00:00", "day", "2017-07-19"),
        (1_752_912_000, "min1", 1_752_912_000),
        ("2025-07-19T00:00:00", "tick30", 1_752_883_200),
    ],
)
def test_prediction_time_matches_chart_contract(value, timeframe, expected):
    assert api._prediction_time(value, timeframe) == expected
