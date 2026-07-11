"""job 조회 + SSE 이벤트 스트림. SSE는 전달 계층일 뿐이며
복구 가능한 상태는 Supabase jobs/job_events에 있다 (docs/06 §4)."""

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import StreamingResponse

from pivot.storage.jobs import TERMINAL_STATUSES, JobTransitionError
from server.deps import job_repo
from server.jobs import stream_job_events

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("/{job_id}")
def get_job(job_id: int) -> dict:
    job = job_repo().get(job_id)
    if job is None:
        raise HTTPException(404, f"job {job_id} not found")
    return job


@router.post("/{job_id}/cancel")
def cancel_job(job_id: int) -> dict:
    """queued/running job을 durable하게 cancelled로 전이한다.

    실행 중인 batch worker는 종목/shard 경계에서 이 상태를 확인하고
    협조적으로 중단한다 (pivot.dataset.batch).
    """
    repo = job_repo()
    job = repo.get(job_id)
    if job is None:
        raise HTTPException(404, f"job {job_id} not found")
    if job["status"] in TERMINAL_STATUSES:
        raise HTTPException(409, f"job {job_id} is already {job['status']}")
    try:
        return repo.finish(job_id, "cancelled")
    except JobTransitionError as exc:  # 경쟁 종료
        raise HTTPException(409, str(exc)) from exc


@router.get("/{job_id}/events")
def job_events(
    job_id: int,
    last_event_id: Annotated[int | None, Header()] = None,
    after_sequence: Annotated[int | None, Query(ge=-1)] = None,
) -> StreamingResponse:
    repo = job_repo()
    if repo.get(job_id) is None:
        raise HTTPException(404, f"job {job_id} not found")
    resume_after = (
        after_sequence
        if after_sequence is not None
        else last_event_id
        if last_event_id is not None
        else -1
    )
    return StreamingResponse(
        stream_job_events(repo, job_id, resume_after),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
    )
