"""M4 학습 run 시작·조회·중단·예측 평가 API."""

from __future__ import annotations

from typing import Annotated, Literal

import pandas as pd
import torch
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from torch.utils.data import DataLoader, Subset

from pivot.config import Timeframe, TrainingConfig
from pivot.dataset.loader import TrainingDatasetError, collate_samples
from pivot.storage.jobs import TERMINAL_STATUSES, JobTransitionError
from pivot.storage.lifecycle import (
    RunDeletionBlockedError,
    RunDeletionFailedError,
    delete_run,
)
from pivot.storage.runs import RunNotFoundError
from pivot.training.evaluate import evaluate_model
from pivot.training.checkpoint import CheckpointError, load_verified_checkpoint
from pivot.training.runs import (
    build_split_datasets,
    dataset_snapshot,
    public_artifact,
    public_evaluation,
    public_run,
)
from server.deps import (
    SHARD_CACHE_ROOT,
    dataset_repo,
    diagnostic_repo,
    job_repo,
    object_storage,
    run_repo,
)
from server.jobs import start_process, stream_job_events
from server.serialize import time_value
from server.training_worker import execute

router = APIRouter(prefix="/api/runs", tags=["runs"])


class StartRunRequest(BaseModel):
    name: str
    dataset_id: int
    config: TrainingConfig


class EvaluateRequest(BaseModel):
    symbol: str
    split: Literal["validation", "test"]


def _prediction_time(value: str | int | float, timeframe: str) -> str | int:
    parsed = (
        pd.to_datetime(value, unit="s")
        if isinstance(value, int | float)
        else pd.Timestamp(value)
    )
    return time_value(parsed, Timeframe.from_code(timeframe))


@router.get("")
def list_runs() -> list[dict]:
    return [public_run(row) for row in run_repo().list()]


@router.post("")
def start_run(request: StartRunRequest) -> dict:
    name = request.name.strip()
    if not name:
        raise HTTPException(422, "name is required")
    datasets = dataset_repo()
    storage = object_storage()
    try:
        # 프로세스를 만들기 전에 세 split과 모든 shard 무결성을 확인한다.
        build_split_datasets(
            datasets,
            storage,
            diagnostic_repo(),
            request.dataset_id,
            SHARD_CACHE_ROOT,
        )
        snapshot = dataset_snapshot(datasets, request.dataset_id)
    except (LookupError, TrainingDatasetError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from exc

    runs = run_repo()
    run = runs.create(
        name=name,
        dataset_id=request.dataset_id,
        config=request.config.model_dump(mode="json"),
        snapshot=snapshot,
    )
    jobs = job_repo()
    try:
        job = jobs.create(
            kind="training",
            payload={"run_id": run["id"], "dataset_id": request.dataset_id},
            total_items=request.config.epochs,
        )
        runs.attach_job(run["id"], job["id"])
        start_process(
            execute,
            run["id"],
            job["id"],
            on_exit=lambda code: _mark_crashed_process(run["id"], job["id"], code),
        )
    except Exception as exc:
        runs.finish(run["id"], "failed", error=f"failed to start training: {exc}")
        if "job" in locals():
            try:
                jobs.fail_active(job["id"], f"failed to start training: {exc}")
            except Exception:
                pass
        raise HTTPException(503, "failed to start durable training process") from exc
    return {"run_id": run["id"], "job_id": job["id"]}


def _mark_crashed_process(run_id: int, job_id: int, exit_code: int) -> None:
    if exit_code == 0:
        return
    message = f"training process exited unexpectedly with code {exit_code}"
    try:
        run_repo().finish(run_id, "failed", error=message)
    except Exception:
        pass
    try:
        job_repo().fail_active(job_id, message)
    except Exception:
        pass


@router.get("/{run_id}")
def get_run(run_id: int) -> dict:
    try:
        detail = run_repo().detail(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {
        "run": public_run(detail["run"]),
        "epochs": detail["epochs"],
        "evaluations": [public_evaluation(row) for row in detail["evaluations"]],
        "artifacts": [public_artifact(row) for row in detail["artifacts"]],
    }


@router.delete("/{run_id}")
def remove_run(run_id: int) -> dict:
    try:
        return delete_run(
            runs=run_repo(),
            jobs=job_repo(),
            storage=object_storage(),
            run_id=run_id,
        )
    except RunNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RunDeletionBlockedError as exc:
        raise HTTPException(409, str(exc)) from exc
    except RunDeletionFailedError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/{run_id}/events")
def run_events(
    run_id: int,
    last_event_id: Annotated[int | None, Header()] = None,
) -> StreamingResponse:
    try:
        run = run_repo().get(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    if run["job_id"] is None:
        raise HTTPException(409, f"run {run_id} has no durable job")
    return StreamingResponse(
        stream_job_events(
            job_repo(),
            run["job_id"],
            last_event_id if last_event_id is not None else -1,
        ),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
    )


@router.post("/{run_id}/stop")
def stop_run(run_id: int) -> dict:
    runs = run_repo()
    try:
        run = runs.get(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    if run["status"] in TERMINAL_STATUSES:
        return {"run_id": run_id, "status": run["status"]}
    job = job_repo().get(run["job_id"]) if run["job_id"] is not None else None
    if job is not None:
        try:
            job_repo().finish(job["id"], "cancelled", error="cancelled by user")
        except JobTransitionError:
            pass
    if job is None or job["status"] == "queued":
        runs.finish(run_id, "cancelled", error="cancelled by user")
    return {"run_id": run_id, "status": "cancelled"}


@router.post("/{run_id}/evaluate")
def prediction_evaluation(run_id: int, request: EvaluateRequest) -> dict:
    runs = run_repo()
    try:
        run = runs.get(run_id)
        artifact = runs.best_artifact(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    if run["status"] != "succeeded":
        raise HTTPException(409, f"run {run_id} is {run['status']!r}, not succeeded")

    datasets = dataset_repo()
    symbols = {
        row["symbol"]: row["split"] for row in datasets.list_symbols(run["dataset_id"])
    }
    if symbols.get(request.symbol) != request.split:
        raise HTTPException(
            409, f"symbol {request.symbol} does not belong to {request.split} split"
        )
    split_dataset = build_split_datasets(
        datasets,
        object_storage(),
        diagnostic_repo(),
        run["dataset_id"],
        SHARD_CACHE_ROOT,
    )[request.split]
    indices = [
        index
        for index in range(len(split_dataset))
        if split_dataset[index].symbol == request.symbol
    ]
    if not indices:
        raise HTTPException(409, f"symbol {request.symbol} has no samples")

    data = object_storage().download(artifact["bucket"], artifact["object_path"])
    try:
        checkpoint = load_verified_checkpoint(
            data, artifact["sha256"], expected_config=run["config"]
        )
    except CheckpointError as exc:
        raise HTTPException(502, str(exc)) from exc
    config = checkpoint.config
    loader = DataLoader(
        Subset(split_dataset, indices),
        batch_size=config.batch_size,
        collate_fn=collate_samples,
    )
    result = evaluate_model(checkpoint.model, loader, torch.device("cpu"))
    timeframe = datasets.get(run["dataset_id"])["timeframe"]
    return {
        "run_id": run_id,
        "dataset_id": run["dataset_id"],
        "symbol": request.symbol,
        "timeframe": timeframe,
        "split": request.split,
        "points": [
            {
                **{key: value for key, value in point.items() if key != "symbol"},
                "time": _prediction_time(point["time"], timeframe),
            }
            for point in result["points"]
        ],
    }
