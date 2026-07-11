"""jobs/job_events repository — durable 작업 상태와 진행 이벤트 (docs/06 §4).

상태 전이는 queued → running → succeeded/failed/cancelled 만 허용하며,
PostgREST 갱신 필터(현재 상태 일치)로 경쟁 없이 강제한다. SSE는 여기 남긴
이벤트를 전달만 한다.
"""

from __future__ import annotations

import datetime

from pivot.storage.supabase import PostgrestClient

JOBS_TABLE = "jobs"
EVENTS_TABLE = "job_events"

TERMINAL_STATUSES = ("succeeded", "failed", "cancelled")


class JobTransitionError(RuntimeError):
    pass


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


class JobRepository:
    def __init__(self, db: PostgrestClient) -> None:
        self.db = db

    def create(self, *, kind: str, payload: dict, total_items: int) -> dict:
        rows = self.db.insert(
            JOBS_TABLE,
            {"kind": kind, "payload": payload, "total_items": total_items},
        )
        return rows[0]

    def get(self, job_id: int) -> dict | None:
        rows = self.db.select(JOBS_TABLE, filters={"id": f"eq.{job_id}"})
        return rows[0] if rows else None

    def list_active(self) -> list[dict]:
        """terminal이 아닌 job 전체 (정리 작업의 stale 판정용)."""
        return self.db.select(
            JOBS_TABLE,
            filters={"status": "in.(queued,running)"},
            order="created_at.asc",
        )

    def mark_running(self, job_id: int) -> dict:
        return self._transition(
            job_id, from_status="queued", values={"status": "running", "started_at": _now()}
        )

    def set_progress(self, job_id: int, completed_items: int) -> None:
        self.db.update(
            JOBS_TABLE,
            {"completed_items": completed_items},
            filters={"id": f"eq.{job_id}"},
        )

    def finish(
        self,
        job_id: int,
        status: str,
        *,
        result: dict | None = None,
        error: str | None = None,
    ) -> dict:
        if status not in TERMINAL_STATUSES:
            raise JobTransitionError(f"{status!r} is not a terminal job status")
        # cancelled는 시작 전(queued)에도 허용한다
        from_status = "queued,running" if status == "cancelled" else "running"
        return self._transition(
            job_id,
            from_status=from_status,
            values={
                "status": status,
                "result": result,
                "error": error,
                "completed_at": _now(),
            },
        )

    def append_event(
        self, job_id: int, sequence: int, event_type: str, payload: dict
    ) -> dict:
        rows = self.db.insert(
            EVENTS_TABLE,
            {
                "job_id": job_id,
                "sequence": sequence,
                "event_type": event_type,
                "payload": payload,
            },
        )
        return rows[0]

    def events_after(self, job_id: int, after_sequence: int = -1) -> list[dict]:
        return self.db.select(
            EVENTS_TABLE,
            filters={"job_id": f"eq.{job_id}", "sequence": f"gt.{after_sequence}"},
            order="sequence.asc",
        )

    def delete(self, job_id: int) -> None:
        """smoke test 정리용 hard delete (job_events는 cascade)."""
        self.db.delete(JOBS_TABLE, filters={"id": f"eq.{job_id}"})

    def _transition(self, job_id: int, *, from_status: str, values: dict) -> dict:
        rows = self.db.update(
            JOBS_TABLE,
            values,
            filters={"id": f"eq.{job_id}", "status": f"in.({from_status})"},
        )
        if not rows:
            current = self.get(job_id)
            state = current["status"] if current else "missing"
            raise JobTransitionError(
                f"job {job_id}: cannot move to {values.get('status')!r} from {state!r}"
            )
        return rows[0]
