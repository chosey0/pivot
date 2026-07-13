from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.routers.live import router
from server.live import LiveService


class FakeLiveService:
    def __init__(self) -> None:
        self.rows = []

    def state(self):
        return {
            "connection": {
                "status": "closed",
                "message": None,
                "last_tick_at": None,
                "last_heartbeat_at": None,
                "market_state": "closed",
            },
            "deployment": None,
            "subscriptions": self.rows,
            "counters": {},
        }

    async def activate_model(self, run_id, artifact_id):
        if run_id == 99:
            raise RuntimeError("private/runs/99/best.pt?token=secret")
        result = self.state()
        result["deployment"] = {
            "id": 3,
            "run_id": run_id,
            "artifact_id": artifact_id,
            "run_name": "ready",
            "dataset_id": 11,
            "dataset_name": "ready-data",
            "model": "cnn1d_temporal_v1",
            "timeframe": "min1",
            "feature_columns": ["Close"],
            "pairing_rule": "adjacent_markers_v1",
            "status": "active",
            "activated_at": "2026-07-13T00:00:00+00:00",
        }
        return result

    def subscriptions(self):
        return self.rows

    async def subscribe(self, symbol):
        if len(symbol) != 6 or not symbol.isdigit():
            raise ValueError("domestic symbol must contain six digits")
        self.rows.append(
            {
                "symbol": symbol,
                "name": None,
                "status": "pending",
                "inference_status": "no_model",
                "error": None,
                "last_tick_at": None,
            }
        )

    async def unsubscribe(self, symbol):
        before = len(self.rows)
        self.rows = [row for row in self.rows if row["symbol"] != symbol]
        if len(self.rows) == before:
            raise KeyError(symbol)


def _client() -> TestClient:
    app = FastAPI()
    app.state.live = FakeLiveService()
    app.include_router(router)
    return TestClient(app)


def test_live_http_mutations_return_ui_contract_state():
    with _client() as client:
        state = client.get("/api/live/state")
        activated = client.put(
            "/api/live/model", json={"run_id": 4, "artifact_id": 7}
        )
        subscribed = client.post(
            "/api/live/subscriptions", json={"symbol": "005930"}
        )
        removed = client.delete("/api/live/subscriptions/005930")

    assert state.status_code == 200
    assert set(state.json()) == {
        "connection",
        "deployment",
        "subscriptions",
        "counters",
    }
    assert activated.json()["deployment"] == {
        "id": 3,
        "run_id": 4,
        "artifact_id": 7,
        "run_name": "ready",
        "dataset_id": 11,
        "dataset_name": "ready-data",
        "model": "cnn1d_temporal_v1",
        "timeframe": "min1",
        "feature_columns": ["Close"],
        "pairing_rule": "adjacent_markers_v1",
        "status": "active",
        "activated_at": "2026-07-13T00:00:00+00:00",
    }
    assert subscribed.json()[0]["inference_status"] == "no_model"
    assert removed.json() == []


def test_live_http_validates_symbol_and_missing_subscription():
    with _client() as client:
        invalid = client.post("/api/live/subscriptions", json={"symbol": "bad"})
        missing = client.delete("/api/live/subscriptions/005930")

    assert invalid.status_code == 422
    assert missing.status_code == 404


def test_live_http_does_not_expose_private_provider_errors():
    with _client() as client:
        response = client.put("/api/live/model", json={"run_id": 99})

    assert response.status_code == 422
    assert response.json() == {"detail": "model activation failed"}
    assert "private" not in response.text
    assert "token" not in response.text


def test_live_websocket_starts_with_snapshot(tmp_path):
    app = FastAPI()
    app.state.live = LiveService(tmp_path)
    app.include_router(router)

    with TestClient(app) as client:
        with client.websocket_connect("/ws/live") as websocket:
            event = websocket.receive_json()

    assert event["type"] == "snapshot"
    assert isinstance(event["sequence"], int)
    assert set(event["data"]) == {
        "connection",
        "deployment",
        "subscriptions",
        "counters",
        "latest_candles",
        "recent_predictions",
    }
