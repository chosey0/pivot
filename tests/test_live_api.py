import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.routers.live import live_events, router
from server.live import LiveService, LiveTarget


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

    async def subscribe(self, symbol, *, name="", region="domestic", exchange=""):
        target = LiveTarget(symbol, name, region, exchange)
        self.rows.append(
            {
                **target.payload(),
                "name": target.name or None,
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

    async def chart_history(self, symbol, timeframe, ma_windows, *, before=None):
        return {
            "symbol": symbol,
            "timeframe": timeframe.code,
            "candles": [],
            "volumes": [],
            "ma": {str(window): [] for window in ma_windows},
            "has_more": False,
            "next_before": None,
        }


def _client() -> TestClient:
    app = FastAPI()
    app.state.live = FakeLiveService()
    app.include_router(router)
    return TestClient(app)


def test_live_http_mutations_return_ui_contract_state():
    with _client() as client:
        state = client.get("/api/live/state")
        activated = client.put("/api/live/model", json={"run_id": 4, "artifact_id": 7})
        subscribed = client.post("/api/live/subscriptions", json={"symbol": "005930"})
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
    assert subscribed.json()[0]["region"] == "domestic"
    assert removed.json() == []


def test_live_http_subscribes_overseas_symbol_with_market_metadata():
    with _client() as client:
        response = client.post(
            "/api/live/subscriptions",
            json={
                "symbol": "AAPL",
                "name": "Apple",
                "region": "overseas",
                "exchange": "ND",
            },
        )

    assert response.status_code == 200
    assert response.json()[0] == {
        "symbol": "AAPL",
        "name": "Apple",
        "region": "overseas",
        "exchange": "ND",
        "status": "pending",
        "inference_status": "no_model",
        "error": None,
        "last_tick_at": None,
    }


def test_live_http_validates_symbol_and_missing_subscription():
    with _client() as client:
        invalid = client.post("/api/live/subscriptions", json={"symbol": "bad"})
        missing = client.delete("/api/live/subscriptions/005930")

    assert invalid.status_code == 422
    assert missing.status_code == 404


def test_live_history_uses_live_api_contract():
    with _client() as client:
        response = client.get(
            "/api/live/history/005930?timeframe=min1&ma=5,20&before=1783904400"
        )

    assert response.status_code == 200
    assert response.json() == {
        "symbol": "005930",
        "timeframe": "min1",
        "candles": [],
        "volumes": [],
        "ma": {"5": [], "20": []},
        "has_more": False,
        "next_before": None,
    }


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


def test_live_websocket_stops_on_disconnect_even_when_send_does_not_fail(tmp_path):
    service = LiveService(tmp_path)

    class SilentSendWebSocket:
        app = type("App", (), {"state": type("State", (), {"live": service})()})()

        async def accept(self):
            pass

        async def send_json(self, event):
            pass

        async def receive(self):
            return {"type": "websocket.disconnect"}

    asyncio.run(asyncio.wait_for(live_events(SilentSendWebSocket()), timeout=0.1))

    assert service._listeners == set()
