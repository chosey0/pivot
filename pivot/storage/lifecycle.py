"""데이터셋/run 삭제와 orphan/stale 정리 (docs/06 §7).

삭제 순서: ① 객체 목록 확정 → ② Storage 객체 삭제 → ③ 성공 후에만 메타데이터
삭제. 시도 전체는 `dataset_delete` job으로 durable하게 남으므로 부분 실패는
확정된 객체 목록과 함께 재시도할 수 있다 (메타데이터가 남아 있어 같은 DELETE
호출이 그대로 재시도가 된다). 정리 작업은 멱등이며 ready 데이터셋과 학습
artifact(pivot-models)는 절대 건드리지 않는다.
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Iterator

from pivot.storage.datasets import DatasetRepository
from pivot.storage.jobs import JobRepository, JobTransitionError
from pivot.storage.runs import RunRepository
from pivot.storage.supabase import DATASET_BUCKET

logger = logging.getLogger(__name__)

# 보수적 age threshold — 로컬 단일 사용자 앱에서 정상 배치가 넘길 수 없는 크기
STALE_JOB_AGE = datetime.timedelta(hours=24)
STALE_DATASET_AGE = datetime.timedelta(hours=24)
ORPHAN_OBJECT_AGE = datetime.timedelta(hours=1)  # 업로드→메타 기록 사이 창 보호


class DatasetDeletionBlockedError(RuntimeError):
    """삭제할 수 없는 상태 (building 등) — 먼저 취소/정리가 필요하다."""


class DatasetDeletionFailedError(RuntimeError):
    """부분 실패 — 실패 원인과 확정 객체 목록이 job에 남아 재시도 가능하다."""


class RunDeletionBlockedError(RuntimeError):
    """실행 중이거나 배포 이력에서 참조 중인 run은 삭제할 수 없다."""


class RunDeletionFailedError(RuntimeError):
    """run 삭제 부분 실패 — 메타데이터와 삭제 job이 남아 재시도 가능하다."""


def delete_dataset(
    *,
    datasets: DatasetRepository,
    jobs: JobRepository,
    storage,
    dataset_id: int,
) -> dict:
    dataset = datasets.get(dataset_id)  # 없으면 DatasetNotFoundError
    if dataset["status"] == "building":
        raise DatasetDeletionBlockedError(
            f"dataset {dataset_id} is building — cancel the batch job first"
        )

    # ① 삭제 대상 객체 목록을 먼저 확정해 job payload에 얼려 둔다.
    #    durable 기록 없이는 삭제를 시작하지 않는다.
    object_paths = [shard["object_path"] for shard in datasets.list_shards(dataset_id)]
    try:
        job = jobs.create(
            kind="dataset_delete",
            payload={
                "dataset_id": dataset_id,
                "dataset_name": dataset["name"],
                "object_paths": object_paths,
            },
            total_items=len(object_paths),
        )
        jobs.mark_running(job["id"])
    except Exception as exc:
        raise DatasetDeletionFailedError(
            f"failed to record the dataset_delete job — nothing was deleted: {exc}"
        ) from exc
    try:
        # ② Storage 객체 삭제 (없는 경로는 무시되므로 재시도에 안전)
        if object_paths:
            storage.remove(DATASET_BUCKET, object_paths)
        # ③ 객체 삭제가 성공한 뒤에만 메타데이터 삭제 (cascade: symbols/shards)
        datasets.delete(dataset_id)
    except Exception as exc:
        jobs.finish(job["id"], "failed", error=str(exc))
        raise DatasetDeletionFailedError(
            f"dataset {dataset_id} deletion failed (retry with the same DELETE call; "
            f"job #{job['id']} keeps the frozen object list): {exc}"
        ) from exc

    jobs.finish(job["id"], "succeeded", result={"deleted_objects": len(object_paths)})
    return {"job_id": job["id"], "deleted_objects": len(object_paths)}


def delete_run(
    *,
    runs: RunRepository,
    jobs: JobRepository,
    storage,
    run_id: int,
) -> dict:
    run = runs.get(run_id)  # 없으면 RunNotFoundError
    if run["status"] in ("queued", "running"):
        raise RunDeletionBlockedError(
            f"run {run_id} is {run['status']} — stop it before deletion"
        )
    deployment_ids = runs.deployment_ids(run_id)
    if deployment_ids:
        raise RunDeletionBlockedError(
            f"run {run_id} is referenced by live deployment {deployment_ids[0]}"
        )

    artifacts = runs.detail(run_id)["artifacts"]
    objects = [
        {"bucket": artifact["bucket"], "object_path": artifact["object_path"]}
        for artifact in artifacts
    ]
    try:
        job = jobs.create(
            kind="run_delete",
            payload={
                "run_id": run_id,
                "run_name": run["name"],
                "objects": objects,
            },
            total_items=len(objects),
        )
        jobs.mark_running(job["id"])
    except Exception as exc:
        raise RunDeletionFailedError(
            f"failed to record the run_delete job — nothing was deleted: {exc}"
        ) from exc

    try:
        by_bucket: dict[str, list[str]] = {}
        for item in objects:
            by_bucket.setdefault(item["bucket"], []).append(item["object_path"])
        for bucket, paths in by_bucket.items():
            storage.remove(bucket, paths)
        runs.delete(run_id)
    except Exception as exc:
        jobs.finish(job["id"], "failed", error=str(exc))
        raise RunDeletionFailedError(
            f"run {run_id} deletion failed (retry with the same DELETE call; "
            f"job #{job['id']} keeps the frozen object list): {exc}"
        ) from exc

    jobs.finish(job["id"], "succeeded", result={"deleted_objects": len(objects)})
    return {"job_id": job["id"], "deleted_objects": len(objects)}


def run_cleanup(
    *,
    datasets: DatasetRepository,
    jobs: JobRepository,
    storage,
    now: datetime.datetime | None = None,
) -> dict:
    """orphan 객체 / stale building 데이터셋 / stale job을 정리한다 (멱등).

    조건부 갱신과 '없으면 건너뜀' 삭제만 사용하므로 반복 실행해도 같은 결과다.
    """
    now = now or datetime.datetime.now(datetime.UTC)
    report: dict = {
        "stale_jobs_cancelled": [],
        "stale_datasets_failed": [],
        "orphan_objects_removed": [],
    }

    # 1) 오래 queued/running에 머문 job → cancelled (durable terminal)
    active_dataset_ids: set[int] = set()
    for job in jobs.list_active():
        reference = _parse_ts(job.get("started_at") or job["created_at"])
        if now - reference >= STALE_JOB_AGE:
            try:
                jobs.finish(job["id"], "cancelled", error="stale job cancelled by cleanup")
                report["stale_jobs_cancelled"].append(job["id"])
            except JobTransitionError:
                pass  # 경쟁 상태로 이미 terminal — 멱등
        else:
            dataset_id = (job.get("payload") or {}).get("dataset_id")
            if dataset_id is not None:
                active_dataset_ids.add(int(dataset_id))

    # 2) 활성 job이 없는데 오래 building에 머문 데이터셋 → failed
    for dataset in datasets.list():
        if dataset["status"] != "building" or dataset["id"] in active_dataset_ids:
            continue
        if now - _parse_ts(dataset["created_at"]) >= STALE_DATASET_AGE:
            try:
                datasets.mark_failed(
                    dataset["id"], "stale building dataset failed by cleanup"
                )
                report["stale_datasets_failed"].append(dataset["id"])
            except Exception:
                logger.exception("failed to fail stale dataset %s", dataset["id"])

    # 3) dataset_shards가 참조하지 않는 오래된 객체 → 삭제.
    #    building 데이터셋 폴더는 업로드가 진행 중일 수 있어 통째로 건너뛴다.
    referenced = datasets.all_shard_paths()
    building_folders = {
        str(dataset["id"])
        for dataset in datasets.list()
        if dataset["status"] == "building"
    }
    orphans: list[str] = []
    for path, created_at in _walk_objects(storage, DATASET_BUCKET, "datasets"):
        parts = path.split("/")
        if len(parts) > 1 and parts[1] in building_folders:
            continue
        if path in referenced:
            continue
        if created_at is None or now - created_at < ORPHAN_OBJECT_AGE:
            continue
        orphans.append(path)
    if orphans:
        storage.remove(DATASET_BUCKET, orphans)
        report["orphan_objects_removed"] = sorted(orphans)
    return report


def _walk_objects(
    storage, bucket: str, prefix: str
) -> Iterator[tuple[str, datetime.datetime | None]]:
    """Storage list API는 폴더 한 단계씩 반환하므로 재귀로 파일만 걷는다.

    Supabase는 폴더를 id 없는 항목으로 반환한다.
    """
    for entry in storage.list_objects(bucket, prefix):
        name = entry.get("name")
        if not name:
            continue
        path = f"{prefix}/{name}"
        if entry.get("id") is None:
            yield from _walk_objects(storage, bucket, path)
        else:
            created_at = entry.get("created_at")
            yield path, _parse_ts(created_at) if created_at else None


def _parse_ts(value: str) -> datetime.datetime:
    parsed = datetime.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed
