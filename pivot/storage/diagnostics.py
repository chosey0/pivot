"""diagnostic_reports repository — 진단 리포트 저장/조회 (docs/06 §1).

리포트는 불변이다: 생성만 하고 수정하지 않는다. 목록 조회는 본문(report)을
제외한 요약 컬럼만 읽는다.
"""

from __future__ import annotations

from pivot.storage.supabase import PostgrestClient

TABLE = "diagnostic_reports"
LIST_COLUMNS = "id,target_type,preset_id,dataset_id,status,summary,created_at"


class ReportNotFoundError(LookupError):
    pass


class DiagnosticReportRepository:
    def __init__(self, db: PostgrestClient) -> None:
        self.db = db

    def create(
        self,
        *,
        target_type: str,
        status: str,
        summary: dict,
        report: dict,
        preset_id: int | None = None,
        dataset_id: int | None = None,
    ) -> dict:
        rows = self.db.insert(
            TABLE,
            {
                "target_type": target_type,
                "status": status,
                "summary": summary,
                "report": report,
                "preset_id": preset_id,
                "dataset_id": dataset_id,
            },
        )
        return rows[0]

    def list(self, *, target_type: str | None = None, limit: int = 50) -> list[dict]:
        filters = {"target_type": f"eq.{target_type}"} if target_type else {}
        return self.db.select(
            TABLE,
            filters=filters,
            order="created_at.desc,id.desc",
            limit=limit,
            columns=LIST_COLUMNS,
        )

    def get(self, report_id: int) -> dict:
        rows = self.db.select(TABLE, filters={"id": f"eq.{report_id}"})
        if not rows:
            raise ReportNotFoundError(f"diagnostic report {report_id} not found")
        return rows[0]

    def delete(self, report_id: int) -> None:
        """smoke test 정리용 hard delete."""
        self.db.delete(TABLE, filters={"id": f"eq.{report_id}"})
