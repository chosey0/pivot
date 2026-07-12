"""실 Supabase ready 데이터셋으로 M4 core 1-epoch 왕복을 검증한다."""

from __future__ import annotations

import argparse
import datetime
import io
from pathlib import Path

import torch

from pivot.config import TrainingConfig
from pivot.storage.datasets import DatasetRepository
from pivot.storage.diagnostics import DiagnosticReportRepository
from pivot.storage.jobs import JobRepository
from pivot.storage.runs import RunRepository
from pivot.storage.supabase import PostgrestClient, StorageObjectClient
from pivot.training.runs import dataset_snapshot, run_training


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset_id", type=int)
    args = parser.parse_args()

    db = PostgrestClient()
    storage = StorageObjectClient()
    datasets = DatasetRepository(db)
    diagnostics = DiagnosticReportRepository(db)
    jobs = JobRepository(db)
    runs = RunRepository(db)
    config = TrainingConfig(
        model="cnn1d_legacy_v1",
        epochs=1,
        batch_size=256,
        sampler="none",
        seed=42,
    )
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d%H%M%S")
    run = runs.create(
        name=f"m4-core-smoke-{stamp}",
        dataset_id=args.dataset_id,
        config=config.model_dump(mode="json"),
        snapshot=dataset_snapshot(datasets, args.dataset_id),
    )
    job = jobs.create(
        kind="training",
        payload={"run_id": run["id"], "dataset_id": args.dataset_id},
        total_items=1,
    )
    runs.attach_job(run["id"], job["id"])
    try:
        run_training(
            runs=runs,
            jobs=jobs,
            datasets=datasets,
            diagnostics=diagnostics,
            storage=storage,
            run_id=run["id"],
            job_id=job["id"],
            cache_root=Path("data/tmp/shards"),
        )
        detail = runs.detail(run["id"])
        assert detail["run"]["status"] == "succeeded", detail["run"]["error"]
        assert len(detail["epochs"]) == 1
        assert len(detail["evaluations"]) == 2
        assert len(detail["artifacts"]) == 1
        artifact = detail["artifacts"][0]
        checkpoint = torch.load(
            io.BytesIO(storage.download(artifact["bucket"], artifact["object_path"])),
            map_location="cpu",
            weights_only=True,
        )
        assert {"state_dict", "config", "feature_columns"} <= checkpoint.keys()
        print(
            {
                "run_id": run["id"],
                "job_id": job["id"],
                "best_epoch": detail["run"]["best_epoch"],
                "best_metric_value": detail["run"]["best_metric_value"],
                "evaluation_splits": [
                    row["metrics"]["split"] for row in detail["evaluations"]
                ],
                "checkpoint_sha256": artifact["sha256"],
            }
        )
    finally:
        detail = runs.detail(run["id"])
        storage.remove(
            "pivot-models", [row["object_path"] for row in detail["artifacts"]]
        )
        db.delete("training_runs", filters={"id": f"eq.{run['id']}"})
        jobs.delete(job["id"])


if __name__ == "__main__":
    main()
