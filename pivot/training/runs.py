"""학습 run 도메인 orchestration."""

from __future__ import annotations

import hashlib
import io
import logging
from pathlib import Path

import torch

from pivot.config import TrainingConfig
from pivot.dataset.loader import ShardDataset
from pivot.models import build_model
from pivot.storage.jobs import JobRepository, JobTransitionError
from pivot.storage.supabase import MODEL_BUCKET
from pivot.training.evaluate import evaluate_model
from pivot.training.train import (
    TrainingCancelled,
    make_loader,
    select_device,
    train_model,
)

logger = logging.getLogger(__name__)


def dataset_snapshot(datasets, dataset_id: int) -> dict:
    dataset = datasets.get(dataset_id)
    return {
        "dataset": dataset,
        "symbols": datasets.list_symbols(dataset_id),
        "shards": [
            {
                key: shard[key]
                for key in (
                    "symbol",
                    "shard_index",
                    "row_count",
                    "size_bytes",
                    "sha256",
                    "feature_schema",
                )
            }
            for shard in datasets.list_shards(dataset_id)
        ],
        "label_mapping": {"0": "fractal_low", "1": "fractal_high", "2": "ignore"},
    }


def build_split_datasets(
    datasets, storage, diagnostics, dataset_id: int, cache_root: Path
) -> dict[str, ShardDataset]:
    result = {
        split: ShardDataset(
            datasets,
            storage,
            dataset_id,
            split,
            cache_root=cache_root,
            diagnostics=diagnostics,
        )
        for split in ("train", "validation", "test")
    }
    for dataset in result.values():
        dataset.verify()
    return result


def run_training(
    *,
    runs,
    jobs: JobRepository,
    datasets,
    diagnostics,
    storage,
    run_id: int,
    job_id: int,
    cache_root: Path,
) -> None:
    emit = _Emitter(jobs, job_id)
    try:
        jobs.mark_running(job_id)
        run = runs.get(run_id)
        config = TrainingConfig.model_validate(run["config"])
        device = select_device()
        runs.mark_running(run_id, str(device))
        emit("run", public_run(runs.get(run_id)))

        splits = build_split_datasets(
            datasets, storage, diagnostics, run["dataset_id"], cache_root
        )
        model = build_model(config.model, len(splits["train"].feature_columns))

        def cancelled() -> bool:
            job = jobs.get(job_id)
            return job is None or job["status"] == "cancelled"

        def require_active() -> None:
            if cancelled():
                raise TrainingCancelled

        def on_epoch(epoch: int, metrics: dict) -> None:
            row = runs.record_epoch(run_id, epoch, metrics)
            jobs.set_progress(job_id, epoch + 1)
            emit("epoch", row)

        trained = train_model(
            model,
            splits["train"],
            splits["validation"],
            config,
            device=device,
            cancelled=cancelled,
            on_epoch=on_epoch,
        )
        require_active()

        for split in ("validation", "test"):
            require_active()
            result = evaluate_model(
                trained["model"],
                make_loader(splits[split], config, training=False),
                device,
            )
            require_active()
            evaluation = runs.create_evaluation(
                run_id, run["dataset_id"], split, result
            )
            emit("evaluation", public_evaluation(evaluation))

        require_active()
        artifact = _save_checkpoint(
            runs=runs,
            storage=storage,
            run=run,
            trained=trained,
            feature_columns=splits["train"].feature_columns,
            cancelled=cancelled,
        )
        emit("artifact", public_artifact(artifact))
        if cancelled():
            storage.remove(artifact["bucket"], [artifact["object_path"]])
            runs.delete_artifact(artifact["id"])
            raise TrainingCancelled
        jobs.finish(job_id, "succeeded", result={"run_id": run_id})
        runs.finish(
            run_id,
            "succeeded",
            best_epoch=trained["best_epoch"],
            best_metric_name=trained["best_metric_name"],
            best_metric_value=trained["best_metric_value"],
        )
        emit("run_succeeded", {"run_id": run_id})
    except TrainingCancelled:
        runs.finish(run_id, "cancelled", error="cancelled by user")
        try:
            jobs.finish(job_id, "cancelled", error="cancelled by user")
        except JobTransitionError:
            pass
        emit("run_cancelled", {"run_id": run_id})
    except JobTransitionError:
        # 시작 전에 API가 job을 취소한 경우.
        runs.finish(run_id, "cancelled", error="cancelled before start")
    except Exception as exc:
        message = f"training failed: {exc}"
        logger.exception(message)
        try:
            runs.finish(run_id, "failed", error=message)
        finally:
            try:
                jobs.finish(job_id, "failed", error=message)
            except Exception:
                pass
        emit("run_failed", {"run_id": run_id, "error": message})


def _save_checkpoint(
    *, runs, storage, run: dict, trained: dict, feature_columns: list[str], cancelled
):
    buffer = io.BytesIO()
    torch.save(
        {
            "state_dict": trained["best_state"],
            "config": run["config"],
            "feature_columns": feature_columns,
            "dataset_snapshot": run["dataset_snapshot"],
        },
        buffer,
    )
    data = buffer.getvalue()
    digest = hashlib.sha256(data).hexdigest()
    path = (
        f"runs/{run['id']}/checkpoints/"
        f"best-{trained['best_epoch']:04d}-{digest[:12]}.pt"
    )
    storage.upload(MODEL_BUCKET, path, data, content_type="application/octet-stream")
    echoed = hashlib.sha256(storage.download(MODEL_BUCKET, path)).hexdigest()
    if echoed != digest:
        raise RuntimeError("checkpoint verification failed after upload")
    try:
        if cancelled():
            raise TrainingCancelled
        return runs.record_artifact(
            run_id=run["id"],
            epoch=trained["best_epoch"],
            kind="best_checkpoint",
            object_path=path,
            size_bytes=len(data),
            sha256=digest,
            metadata={
                "model": run["config"]["model"],
                "feature_columns": feature_columns,
                "best_metric_name": trained["best_metric_name"],
                "best_metric_value": trained["best_metric_value"],
            },
        )
    except Exception:
        storage.remove(MODEL_BUCKET, [path])
        raise


def public_run(run: dict) -> dict:
    hidden = {"dataset_snapshot"}
    return {key: value for key, value in run.items() if key not in hidden}


def public_artifact(artifact: dict) -> dict:
    hidden = {"bucket", "object_path", "content_type", "run_id"}
    return {key: value for key, value in artifact.items() if key not in hidden}


def public_evaluation(evaluation: dict) -> dict:
    metrics = dict(evaluation["metrics"])
    split = metrics.pop("split")
    return {**evaluation, "split": split, "metrics": metrics}


class _Emitter:
    def __init__(self, jobs: JobRepository, job_id: int) -> None:
        self.jobs = jobs
        self.job_id = job_id
        self.sequence = 0

    def __call__(self, event_type: str, payload: dict) -> None:
        try:
            self.jobs.append_event(self.job_id, self.sequence, event_type, payload)
        except Exception:
            logger.exception(
                "failed to persist %s event for run job %s", event_type, self.job_id
            )
        else:
            self.sequence += 1
