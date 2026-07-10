"""장기 작업 실행 + SSE 스트리밍 (docs/04 §3).

durable 상태는 전부 Supabase(jobs/job_events)에 있고, 여기서는
백그라운드 스레드 실행과 이벤트 전달(SSE)만 담당한다. 전처리는 pandas
CPU 작업이라 이벤트 루프 밖 스레드에서 돌린다.
"""

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Callable

from pivot.storage.jobs import TERMINAL_STATUSES, JobRepository

POLL_INTERVAL_SECONDS = 1.0


def start_background(target: Callable[[], None]) -> None:
    threading.Thread(target=target, daemon=True).start()


def _sse(event_type: str, payload: dict, *, event_id: int | None = None) -> str:
    id_line = f"id: {event_id}\n" if event_id is not None else ""
    return (
        f"{id_line}event: {event_type}\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    )


def _job_snapshot(job: dict) -> dict:
    return {
        "id": job["id"],
        "kind": job["kind"],
        "status": job["status"],
        "completed_items": job["completed_items"],
        "total_items": job["total_items"],
        "error": job["error"],
        "result": job["result"],
    }


async def stream_job_events(
    repo: JobRepository, job_id: int, after_sequence: int = -1
) -> AsyncIterator[str]:
    """지정 sequence 이후 이벤트를 흘려보내고, 종료 상태면 스트림을 닫는다."""
    last_sequence = after_sequence
    while True:
        events = await asyncio.to_thread(repo.events_after, job_id, last_sequence)
        for event in events:
            last_sequence = event["sequence"]
            yield _sse(
                event["event_type"],
                {"sequence": event["sequence"], **event["payload"]},
                event_id=event["sequence"],
            )

        job = await asyncio.to_thread(repo.get, job_id)
        if job is None:
            return
        yield _sse("job", _job_snapshot(job))
        if job["status"] in TERMINAL_STATUSES:
            # 종료 후 기록된 잔여 이벤트까지 비우고 닫는다
            events = await asyncio.to_thread(repo.events_after, job_id, last_sequence)
            for event in events:
                yield _sse(
                    event["event_type"],
                    {"sequence": event["sequence"], **event["payload"]},
                    event_id=event["sequence"],
                )
            return
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
