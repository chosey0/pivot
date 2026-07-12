"""training_runs/epochs/evaluations/artifacts Supabase repository."""

from __future__ import annotations

import datetime

from pivot.storage.supabase import MODEL_BUCKET, PostgrestClient


class RunNotFoundError(LookupError):
    pass


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


class RunRepository:
    def __init__(self, db: PostgrestClient) -> None:
        self.db = db

    def create(
        self, *, name: str, dataset_id: int, config: dict, snapshot: dict
    ) -> dict:
        return self.db.insert(
            "training_runs",
            {
                "name": name,
                "dataset_id": dataset_id,
                "config": config,
                "dataset_snapshot": snapshot,
            },
        )[0]

    def attach_job(self, run_id: int, job_id: int) -> dict:
        return self._update(run_id, {"job_id": job_id}, status="queued")

    def get(self, run_id: int) -> dict:
        rows = self.db.select("training_runs", filters={"id": f"eq.{run_id}"})
        if not rows:
            raise RunNotFoundError(f"run {run_id} not found")
        return {**rows[0], "dataset_name": self._dataset_name(rows[0]["dataset_id"])}

    def list(self) -> list[dict]:
        runs = self.db.select("training_runs", order="created_at.desc")
        return [
            {**run, "dataset_name": self._dataset_name(run["dataset_id"])}
            for run in runs
        ]

    def mark_running(self, run_id: int, device: str) -> dict:
        return self._update(
            run_id,
            {"status": "running", "device": device, "started_at": _now()},
            status="queued",
        )

    def finish(
        self,
        run_id: int,
        status: str,
        *,
        best_epoch: int | None = None,
        best_metric_name: str | None = None,
        best_metric_value: float | None = None,
        error: str | None = None,
    ) -> dict:
        if status not in ("succeeded", "failed", "cancelled"):
            raise ValueError("invalid terminal run status")
        current = self.get(run_id)
        if current["status"] in ("succeeded", "failed", "cancelled"):
            return current
        return self._update(
            run_id,
            {
                "status": status,
                "best_epoch": best_epoch,
                "best_metric_name": best_metric_name,
                "best_metric_value": best_metric_value,
                "error": error,
                "completed_at": _now(),
            },
            status=current["status"],
        )

    def record_epoch(self, run_id: int, epoch: int, metrics: dict) -> dict:
        return self.db.insert(
            "training_epochs", {"run_id": run_id, "epoch": epoch, "metrics": metrics}
        )[0]

    def create_evaluation(
        self, run_id: int, dataset_id: int, split: str, result: dict
    ) -> dict:
        metrics = {**result["metrics"], "split": split}
        confusion = metrics.pop("confusion_matrix")
        per_class = metrics.pop("per_class_metrics")
        return self.db.insert(
            "evaluations",
            {
                "run_id": run_id,
                "dataset_id": dataset_id,
                "metrics": metrics,
                "confusion_matrix": confusion,
                "per_class_metrics": per_class,
            },
        )[0]

    def record_artifact(
        self,
        *,
        run_id: int,
        kind: str,
        object_path: str,
        size_bytes: int,
        sha256: str,
        metadata: dict,
        epoch: int | None = None,
        content_type: str = "application/octet-stream",
    ) -> dict:
        return self.db.insert(
            "training_artifacts",
            {
                "run_id": run_id,
                "epoch": epoch,
                "kind": kind,
                "bucket": MODEL_BUCKET,
                "object_path": object_path,
                "content_type": content_type,
                "size_bytes": size_bytes,
                "sha256": sha256,
                "metadata": metadata,
            },
        )[0]

    def detail(self, run_id: int) -> dict:
        run = self.get(run_id)
        return {
            "run": run,
            "epochs": self.db.select(
                "training_epochs", filters={"run_id": f"eq.{run_id}"}, order="epoch.asc"
            ),
            "evaluations": self.db.select(
                "evaluations",
                filters={"run_id": f"eq.{run_id}"},
                order="created_at.asc",
            ),
            "artifacts": self.db.select(
                "training_artifacts",
                filters={"run_id": f"eq.{run_id}"},
                order="created_at.asc",
            ),
        }

    def best_artifact(self, run_id: int) -> dict:
        rows = self.db.select(
            "training_artifacts",
            filters={"run_id": f"eq.{run_id}", "kind": "eq.best_checkpoint"},
            order="created_at.desc",
            limit=1,
        )
        if not rows:
            raise RunNotFoundError(f"run {run_id} has no best checkpoint")
        return rows[0]

    def delete_artifact(self, artifact_id: int) -> None:
        self.db.delete("training_artifacts", filters={"id": f"eq.{artifact_id}"})

    def _update(self, run_id: int, values: dict, *, status: str) -> dict:
        rows = self.db.update(
            "training_runs",
            values,
            filters={"id": f"eq.{run_id}", "status": f"eq.{status}"},
        )
        if not rows:
            raise RuntimeError(f"run {run_id} cannot transition from {status}")
        return rows[0]

    def _dataset_name(self, dataset_id: int) -> str:
        rows = self.db.select(
            "datasets", filters={"id": f"eq.{dataset_id}"}, columns="name"
        )
        return rows[0]["name"] if rows else ""
