"""job 조회 + SSE 이벤트 스트림. SSE는 전달 계층일 뿐이며
복구 가능한 상태는 Supabase jobs/job_events에 있다 (docs/06 §4)."""

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import StreamingResponse

from server.deps import job_repo
from server.jobs import stream_job_events

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("/{job_id}")
def get_job(job_id: int) -> dict:
    job = job_repo().get(job_id)
    if job is None:
        raise HTTPException(404, f"job {job_id} not found")
    return job


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
