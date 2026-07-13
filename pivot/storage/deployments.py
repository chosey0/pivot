"""Single-active live model deployment repository."""

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
