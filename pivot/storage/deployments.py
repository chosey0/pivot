"""Single-active live model deployment repository."""

import datetime as dt

from pivot.storage.supabase import PostgrestClient


class DeploymentRepository:
    def __init__(self, db: PostgrestClient) -> None:
        self.db = db

    def active(self) -> dict | None:
        rows = self.db.select(
            "live_deployments",
            filters={"active": "eq.true"},
            order="activated_at.desc",
            limit=1,
        )
        return rows[0] if rows else None

    def activate(self, *, run_id: int, artifact_id: int) -> dict:
        rows = self.db.rpc(
            "activate_live_deployment",
            {"target_run_id": run_id, "target_artifact_id": artifact_id},
        )
        if len(rows) != 1:
            raise RuntimeError("activation did not return one live deployment")
        return rows[0]

    def deactivate(self) -> dict | None:
        rows = self.db.update(
            "live_deployments",
            {
                "active": False,
                "deactivated_at": dt.datetime.now(dt.UTC).isoformat(),
            },
            filters={"active": "eq.true"},
        )
        return rows[0] if rows else None
