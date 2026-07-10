"""durable job SSE가 event id를 사용해 중단 지점부터 재개되는지 검증한다."""

import asyncio

from pivot.storage.jobs import JobRepository
from server.jobs import stream_job_events

from fakes import FakeDb


async def _collect(stream) -> list[str]:
    return [chunk async for chunk in stream]


def test_stream_resumes_after_last_event_id():
    repo = JobRepository(FakeDb())
    job = repo.create(kind="preprocess_batch", payload={}, total_items=1)
    repo.mark_running(job["id"])
    for sequence in range(3):
        repo.append_event(job["id"], sequence, "tick", {"value": sequence})
    repo.finish(job["id"], "succeeded")

    chunks = asyncio.run(
        _collect(stream_job_events(repo, job["id"], after_sequence=1))
    )

    durable = [chunk for chunk in chunks if "event: tick" in chunk]
    assert durable == ['id: 2\nevent: tick\ndata: {"sequence": 2, "value": 2}\n\n']
    assert any("event: job" in chunk and '"status": "succeeded"' in chunk for chunk in chunks)
