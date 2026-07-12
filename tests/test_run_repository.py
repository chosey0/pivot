from pivot.storage.runs import RunRepository

from fakes import FakeDb


def make_dataset(db: FakeDb) -> int:
    return db.insert(
        "datasets",
        {
            "name": "ready-set",
            "preset_id": 1,
            "preset_snapshot": {},
            "timeframe": "day",
            "feature_columns": ["Close"],
            "status": "ready",
        },
    )[0]["id"]


def test_run_repository_records_reproducible_history():
    db = FakeDb()
    dataset_id = make_dataset(db)
    repo = RunRepository(db)
    run = repo.create(
        name="baseline", dataset_id=dataset_id, config={"seed": 42}, snapshot={"x": 1}
    )
    repo.attach_job(run["id"], 9)
    repo.mark_running(run["id"], "cpu")
    repo.record_epoch(run["id"], 0, {"validation_macro_f1": 0.4})
    repo.create_evaluation(
        run["id"],
        dataset_id,
        "validation",
        {
            "metrics": {
                "accuracy": 0.5,
                "macro_f1": 0.4,
                "confusion_matrix": [[1, 0, 0], [0, 1, 0], [1, 1, 0]],
                "per_class_metrics": {},
            }
        },
    )
    repo.record_artifact(
        run_id=run["id"],
        kind="best_checkpoint",
        object_path="runs/1/best.pt",
        size_bytes=10,
        sha256="a" * 64,
        metadata={"model": "cnn1d_legacy_v1"},
        epoch=0,
    )
    repo.finish(
        run["id"],
        "succeeded",
        best_epoch=0,
        best_metric_name="val_macro_f1",
        best_metric_value=0.4,
    )

    detail = repo.detail(run["id"])
    assert detail["run"]["status"] == "succeeded"
    assert detail["epochs"][0]["epoch"] == 0
    assert detail["evaluations"][0]["metrics"]["split"] == "validation"
    assert detail["artifacts"][0]["sha256"] == "a" * 64
    assert repo.list()[0]["dataset_name"] == "ready-set"
