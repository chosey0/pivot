"""durable job SSE가 event id를 사용해 중단 지점부터 재개되는지 검증한다."""

import asyncio

from pivot.storage.jobs import JobRepository
from server import jobs as server_jobs
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


def test_stop_processes_terminates_spawned_children(monkeypatch):
    class FakeProcess:
        pid = 123

        def __init__(self, **kwargs):
            self.alive = False
            self.terminated = False

        def start(self):
            self.alive = True

        def is_alive(self):
            return self.alive

        def terminate(self):
            self.terminated = True
            self.alive = False

        def join(self, timeout=None):
            pass

        def kill(self):
            self.alive = False

    process = FakeProcess()
    context = type("Context", (), {"Process": lambda self, **kwargs: process})()
    monkeypatch.setattr(server_jobs.multiprocessing, "get_context", lambda _: context)

    server_jobs.start_process(lambda: None)
    server_jobs.stop_processes()

    assert process.terminated
    assert not server_jobs._processes
