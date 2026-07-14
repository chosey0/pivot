import asyncio
import datetime as dt
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import pandas as pd

from pivot.config import PreprocessPreset, Timeframe
from pivot.realtime.aggregate import CandleAggregator
from pivot.realtime.infer import LivePrediction
from server import live as live_module
from server.live import KST, LiveListener, LiveService, SubscriptionStore, trade_from_tick


class EmptyDeployments:
    def active(self):
        return None


class ActivatingDeployments(EmptyDeployments):
    def __init__(self) -> None:
        self.calls = []

    def activate(self, *, run_id, artifact_id):
        self.calls.append((run_id, artifact_id))
        return {
            "id": len(self.calls),
            "run_id": run_id,
            "artifact_id": artifact_id,
            "activated_at": "2026-07-13T00:00:00+00:00",
        }


class DeployableRuns:
    def get(self, run_id):
        return {
            "id": run_id,
            "name": "live-run",
            "dataset_id": 9,
            "dataset_name": "live-data",
            "status": "succeeded",
            "config": {"model": "cnn1d_temporal_v1"},
            "dataset_snapshot": {
                "dataset": {
                    "timeframe": "min1",
                    "feature_columns": ["Close"],
                    "preset_snapshot": {
                        "preset": {
                            "labeling": {"sample_pairing": "adjacent_markers_v1"}
                        }
                    },
                }
            },
        }

    def best_artifact(self, run_id):
        return self.artifact(run_id, 5)

    def artifact(self, run_id, artifact_id):
        return {
            "id": artifact_id,
            "run_id": run_id,
            "bucket": "pivot-models",
            "object_path": "private/best.pt",
            "sha256": "a" * 64,
        }


class FakeSession:
    def __init__(self) -> None:
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.events: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def close(self):
        self.closed = True

    async def subscribe_trades(self, symbol: str):
        self.subscribed.append(symbol)

    async def unsubscribe(self, symbol: str):
        self.unsubscribed.append(symbol)

    async def stream(self):
        while True:
            yield await self.events.get()


class FakeClient:
    def __init__(self, session: FakeSession) -> None:
        self.realtime = SimpleNamespace(session=lambda: session)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class StubEngine:
    def __init__(self, timeframe: str = "min1") -> None:
        self.preset = PreprocessPreset(timeframe=Timeframe.from_code(timeframe))

    def infer(self, symbol, frame):
        return None

    def warmup(self):
        return None


class PredictingEngine(StubEngine):
    def __init__(self) -> None:
        super().__init__()
        self.seen = set()

    def infer(self, symbol, frame):
        closed_time = frame.index[-1]
        if closed_time in self.seen:
            return None
        self.seen.add(closed_time)
        return LivePrediction(
            deployment_id=1,
            symbol=symbol,
            timeframe="min1",
            closed_time=closed_time,
            scores=[0.2, 0.7, 0.1],
            selected_class=1,
            candidates=[],
        )


async def _eventually(predicate, timeout: float = 1.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def _tick(symbol: str, exchange_ts: str, seq: int, price: str = "70000"):
    return SimpleNamespace(
        tr_id="0B",
        symbol=symbol,
        exchange_ts=exchange_ts,
        received_at=f"2026-07-13T00:00:{seq:02d}+00:00",
        received_seq=seq,
        price=Decimal(price),
        volume=10,
    )


def test_subscription_store_is_sorted_atomic_and_validates_symbols(tmp_path: Path):
    store = SubscriptionStore(tmp_path / "meta" / "live_subscriptions.json")
    store.save({"005930", "000660"})

    assert store.load() == {"000660", "005930"}
    assert not store.path.with_suffix(".tmp").exists()
    store.path.write_text('["bad"]', encoding="utf-8")
    with pytest.raises(ValueError, match="six digits"):
        store.load()


def test_trade_from_tick_combines_exchange_time_with_received_kst_date():
    trade = trade_from_tick(_tick("005930", "09:00:01", 1))

    assert trade.exchange_ts == dt.datetime(2026, 7, 13, 9, 0, 1, tzinfo=KST)
    assert trade.received_at.astimezone(KST).date() == dt.date(2026, 7, 13)
    assert trade.price == Decimal("70000")


def test_gateway_restores_and_mutates_subscriptions_on_one_session(tmp_path: Path):
    async def scenario():
        store = SubscriptionStore(tmp_path / "live.json")
        store.save({"005930"})
        session = FakeSession()
        created = 0

        def client_factory():
            nonlocal created
            created += 1
            return FakeClient(session)

        service = LiveService(
            tmp_path,
            deployments=EmptyDeployments(),
            runs=object(),
            storage=object(),
            client_factory=client_factory,
            subscription_path=store.path,
            heartbeat_interval=60,
        )
        listener = await service.add_listener()
        await service.start()
        try:
            await _eventually(lambda: session.subscribed == ["005930"])
            await service.subscribe("000660")
            await service.unsubscribe("005930")

            assert created == 1
            assert session.subscribed == ["005930", "000660"]
            assert session.unsubscribed == ["005930"]
            assert store.load() == {"000660"}
            subscription_events = [
                event for event in listener.events if event["type"] == "subscription"
            ]
            assert subscription_events
            assert all(
                set(event["data"]) == {"symbol", "status", "error"}
                for event in subscription_events
            )
        finally:
            await service.close()

        assert session.closed

    asyncio.run(scenario())


def test_close_releases_session_before_cancelling_gateway(tmp_path: Path):
    async def scenario():
        session = FakeSession()
        cancelled_after_close = False

        async def gateway():
            nonlocal cancelled_after_close
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled_after_close = session.closed
                raise

        service = LiveService(tmp_path)
        service._session = session
        service._gateway_task = asyncio.create_task(gateway())
        await asyncio.sleep(0)

        await service.close()

        assert session.closed
        assert cancelled_after_close
        assert service._gateway_task is None
        assert service._connection == "closed"

    asyncio.run(scenario())


def test_dynamic_subscription_reconciles_cache_for_active_model(
    tmp_path: Path, monkeypatch
):
    reconciled = []

    async def update(client, symbol, timeframe, data_root):
        reconciled.append((symbol, timeframe.code, data_root))
        return pd.DataFrame(
            {
                "Open": [100.0],
                "High": [101.0],
                "Low": [99.0],
                "Close": [100.0],
                "Volume": [10],
                "Amount": [1000.0],
            },
            index=pd.DatetimeIndex(["2026-07-13 09:00:00"], name="Time"),
        )

    async def scenario():
        service = LiveService(
            tmp_path,
            deployments=EmptyDeployments(),
            runs=object(),
            storage=object(),
        )
        service._client = object()
        service._session = FakeSession()
        service._gateway_task = asyncio.current_task()
        await service._install_engine(
            StubEngine(),
            {
                "id": 1,
                "run_id": 1,
                "artifact_id": 1,
                "run_name": "stub",
                "timeframe": "min1",
                "feature_columns": [],
                "model": "stub",
                "activated_at": None,
            },
        )

        await service.subscribe("005930")

        assert reconciled == [("005930", "min1", tmp_path)]
        assert service.stats["reconciliations"] == 1

    monkeypatch.setattr(live_module, "update_cache", update)
    asyncio.run(scenario())


def test_live_minute_history_fetches_kiwoom_and_returns_only_today(
    tmp_path: Path, monkeypatch
):
    today = dt.datetime.now(KST).date()
    yesterday = today - dt.timedelta(days=1)
    calls = []

    def bar(day: dt.date, minute: str, price: str):
        return SimpleNamespace(
            timestamp=f"{day:%Y%m%d}{minute}",
            open=Decimal(price),
            high=Decimal(price),
            low=Decimal(price),
            close=Decimal(price),
            volume=10,
            amount=Decimal("1000"),
        )

    async def fetch(client, symbol, timeframe, **kwargs):
        calls.append((client, symbol, timeframe.code, kwargs))
        return [
            bar(yesterday, "153000", "99"),
            bar(today, "090000", "100"),
            bar(today, "090100", "101"),
        ]

    async def scenario():
        service = LiveService(tmp_path)
        service._desired = {"005930"}
        service._client = object()

        result = await service.chart_history(
            "005930", Timeframe.from_code("min1"), (2,)
        )

        assert result["timeframe"] == "min1"
        assert len(result["candles"]) == 2
        assert result["has_more"] is True
        assert len(result["ma"]["2"]) == 2
        assert result["candles"][-1]["time"] == int(
            dt.datetime.combine(today, dt.time(9, 1), tzinfo=dt.UTC).timestamp()
        )

    monkeypatch.setattr(live_module, "fetch_bars", fetch)
    asyncio.run(scenario())

    assert calls[0][1:3] == ("005930", "min1")
    assert calls[0][3]["start_date"].endswith(" 000000")
    assert calls[0][3]["end_date"] == today


def test_live_history_uses_active_training_preset_for_fractal_markers(
    tmp_path: Path, monkeypatch
):
    today = dt.datetime.now(KST).date()
    prices = [100, 105, 101, 99, 102]

    async def fetch(*args, **kwargs):
        return [
            SimpleNamespace(
                timestamp=f"{today:%Y%m%d}09{index:02d}00",
                open=Decimal(price),
                high=Decimal(price),
                low=Decimal(price),
                close=Decimal(price),
                volume=10,
                amount=Decimal("1000"),
            )
            for index, price in enumerate(prices)
        ]

    async def scenario():
        service = LiveService(tmp_path)
        service._desired = {"005930"}
        service._client = object()
        service._engine = StubEngine()
        service._engine.preset = PreprocessPreset(
            timeframe=Timeframe.from_code("min1"),
            fractal={"n": 3, "tie_policy": "plateau_last"},
        )

        result = await service.chart_history(
            "005930", Timeframe.from_code("min1"), ()
        )

        assert [(row["kind"], row["label"]) for row in result["fractal_markers"]] == [
            ("high", 1),
            ("low", 0),
        ]

    monkeypatch.setattr(live_module, "fetch_bars", fetch)
    asyncio.run(scenario())


def test_minute_boundary_emits_closed_before_next_update(tmp_path: Path):
    async def scenario():
        service = LiveService(
            tmp_path,
            deployments=EmptyDeployments(),
            runs=object(),
            storage=object(),
        )
        service._desired = {"005930"}
        service._sync_subscription_state()
        await service._install_engine(
            StubEngine(),
            {
                "id": 1,
                "run_id": 1,
                "artifact_id": 1,
                "run_name": "stub",
                "timeframe": "min1",
                "feature_columns": [],
                "model": "stub",
                "activated_at": None,
            },
        )
        service._aggregators[("005930", "min1")] = CandleAggregator(
            "005930",
            Timeframe.from_code("min1"),
            observing_since=dt.datetime(2026, 7, 13, 8, 59, tzinfo=KST),
        )
        listener = await service.add_listener()

        await service.handle_sdk_event(_tick("005930", "09:00:01", 1, "70000"))
        await service.handle_sdk_event(_tick("005930", "09:01:01", 2, "70100"))

        events = [
            event
            for event in list(listener.events)
            if event["type"] == "snapshot" or event["data"].get("timeframe") == "min1"
        ]
        assert [event["type"] for event in events] == [
            "snapshot",
            "candle_update",
            "candle_closed",
            "candle_update",
        ]
        assert events[2]["data"]["candle"]["close"] == 70000.0
        assert events[3]["data"]["candle"]["open"] == 70100.0
        assert events[1]["data"]["candle"]["time"] == int(
            dt.datetime(2026, 7, 13, 9, 0, tzinfo=dt.UTC).timestamp()
        )

        snapshot = service.snapshot()["latest_candles"]
        assert {entry["timeframe"] for entry in snapshot} == {"day", "min1"}

    asyncio.run(scenario())


def test_partial_first_minute_is_suppressed_until_next_bucket(tmp_path: Path):
    async def scenario():
        service = LiveService(tmp_path)
        service._desired = {"005930"}
        service._sync_subscription_state()
        listener = await service.add_listener()

        await service.handle_sdk_event(_tick("005930", "09:00:30", 30, "70000"))
        await service.handle_sdk_event(_tick("005930", "09:01:01", 31, "70100"))

        events = [
            event
            for event in listener.events
            if event["type"].startswith("candle_")
            and event["data"].get("timeframe") == "min1"
        ]
        assert [event["type"] for event in events] == ["candle_update"]
        assert events[0]["data"]["candle"]["open"] == 70100.0

    asyncio.run(scenario())


def test_listener_coalesces_updates_and_preserves_critical_events():
    async def scenario():
        listener = LiveListener(maxsize=2)
        update1 = {"type": "candle_update", "data": {"symbol": "005930", "v": 1}}
        update2 = {"type": "candle_update", "data": {"symbol": "005930", "v": 2}}
        closed = {"type": "candle_closed", "data": {"symbol": "005930"}}
        prediction = {"type": "prediction", "data": {"symbol": "005930"}}

        assert await listener.put(update1)
        assert await listener.put(update2)
        assert len(listener.events) == 1
        assert listener.events[0]["data"]["v"] == 2
        assert await listener.put(closed)
        assert await listener.put(prediction)

        assert [event["type"] for event in listener.events] == [
            "candle_closed",
            "prediction",
        ]

    asyncio.run(scenario())


def test_recorded_ticks_replay_deterministically_without_duplicate_prediction(
    tmp_path: Path,
):
    async def replay(root: Path):
        service = LiveService(
            root,
            deployments=EmptyDeployments(),
            runs=object(),
            storage=object(),
        )
        service._desired = {"005930"}
        service._sync_subscription_state()
        await service._install_engine(
            PredictingEngine(),
            {
                "id": 1,
                "run_id": 1,
                "artifact_id": 1,
                "run_name": "stub",
                "timeframe": "min1",
                "feature_columns": [],
                "model": "stub",
                "activated_at": None,
            },
        )
        listener = await service.add_listener()
        ticks = [
            _tick("005930", "09:00:01", 1, "70000"),
            _tick("005930", "09:00:20", 2, "70200"),
            _tick("005930", "09:01:01", 3, "70100"),
            _tick("005930", "09:02:01", 4, "70300"),
        ]
        for tick in ticks:
            await service.handle_sdk_event(tick)
        first_count = len(service._predictions)
        for tick in ticks:
            await service.handle_sdk_event(tick)
        assert len(service._predictions) == first_count == 1
        return [
            {"type": event["type"], "data": event["data"]}
            for event in listener.events
            if event["type"] in {"candle_closed", "prediction"}
        ]

    first = asyncio.run(replay(tmp_path / "first"))
    second = asyncio.run(replay(tmp_path / "second"))
    assert first == second


def test_model_activation_validates_before_pointer_swap_and_returns_public_state(
    tmp_path: Path, monkeypatch
):
    order = []

    class FakeEngine(StubEngine):
        def __init__(self, checkpoint, *, deployment_id, device):
            super().__init__()
            self.deployment_id = deployment_id
            self.checkpoint = checkpoint

        def warmup(self):
            order.append("warmup")
            if self.checkpoint == "bad-warmup":
                raise RuntimeError("warmup failed")

    async def scenario():
        deployments = ActivatingDeployments()
        service = LiveService(
            tmp_path,
            deployments=deployments,
            runs=DeployableRuns(),
            storage=object(),
        )

        async def load_ok(*args):
            return object()

        service._load_checkpoint = load_ok
        state = await service.activate_model(3, 5)

        order.append("activated" if deployments.calls else "not-activated")
        assert order[:2] == ["warmup", "activated"]
        assert deployments.calls == [(3, 5)]
        assert state["deployment"]["pairing_rule"] == "adjacent_markers_v1"
        assert state["deployment"]["dataset_name"] == "live-data"
        assert "object_path" not in str(state)

        async def load_bad(*args):
            return "bad-warmup"

        service._load_checkpoint = load_bad
        with pytest.raises(RuntimeError, match="warmup failed"):
            await service.activate_model(4, 6)
        assert deployments.calls == [(3, 5)]
        assert service.state()["deployment"]["run_id"] == 3

    monkeypatch.setattr(live_module, "LiveInferenceEngine", FakeEngine)
    asyncio.run(scenario())


def test_reconcile_is_idempotent_for_unchanged_last_closed_bar(tmp_path: Path, monkeypatch):
    async def scenario():
        service = LiveService(
            tmp_path,
            deployments=EmptyDeployments(),
            runs=object(),
            storage=object(),
        )
        service._desired = {"005930"}
        service._sync_subscription_state()
        await service._install_engine(
            PredictingEngine(),
            {
                "id": 1,
                "run_id": 1,
                "artifact_id": 1,
                "run_name": "stub",
                "timeframe": "min1",
                "feature_columns": [],
                "model": "stub",
                "activated_at": None,
            },
        )
        service._client = object()
        await service._reconcile_all()
        await service._reconcile_all()

        assert service.stats["reconciliations"] == 2
        assert len(service._predictions) == 1

    frame = pd.DataFrame(
        {
            "Open": [100.0],
            "High": [101.0],
            "Low": [99.0],
            "Close": [100.0],
            "Volume": [10],
            "Amount": [1000.0],
        },
        index=pd.DatetimeIndex(["2026-07-13 09:00:00"], name="Time"),
    )

    async def update(*args, **kwargs):
        return frame

    monkeypatch.setattr(live_module, "update_cache", update)
    asyncio.run(scenario())


def test_reconcile_does_not_emit_after_symbol_is_unsubscribed(tmp_path: Path, monkeypatch):
    holder = {}

    async def scenario():
        service = LiveService(
            tmp_path,
            deployments=EmptyDeployments(),
            runs=object(),
            storage=object(),
        )
        holder["service"] = service
        service._desired = {"005930"}
        service._sync_subscription_state()
        await service._install_engine(
            PredictingEngine(),
            {
                "id": 1,
                "run_id": 1,
                "artifact_id": 1,
                "run_name": "stub",
                "timeframe": "min1",
                "feature_columns": [],
                "model": "stub",
                "activated_at": None,
            },
        )
        service._client = object()
        listener = await service.add_listener()
        await service._reconcile_all()

        assert service.stats["reconciliations"] == 0
        assert not service._predictions
        assert all(
            event["type"] not in {"prediction", "warmup", "error"}
            for event in listener.events
        )

    frame = pd.DataFrame(
        {
            "Open": [100.0],
            "High": [101.0],
            "Low": [99.0],
            "Close": [100.0],
            "Volume": [10],
            "Amount": [1000.0],
        },
        index=pd.DatetimeIndex(["2026-07-13 09:00:00"], name="Time"),
    )

    async def update(*args, **kwargs):
        holder["service"]._desired.clear()
        return frame

    monkeypatch.setattr(live_module, "update_cache", update)
    asyncio.run(scenario())


def test_reconcile_does_not_emit_after_model_swap(tmp_path: Path, monkeypatch):
    holder = {}

    async def scenario():
        service = LiveService(
            tmp_path,
            deployments=EmptyDeployments(),
            runs=object(),
            storage=object(),
        )
        holder["service"] = service
        service._desired = {"005930"}
        service._sync_subscription_state()
        await service._install_engine(
            PredictingEngine(),
            {
                "id": 1,
                "run_id": 1,
                "artifact_id": 1,
                "run_name": "old",
                "timeframe": "min1",
                "feature_columns": [],
                "model": "stub",
                "activated_at": None,
            },
        )
        service._client = object()
        await service._reconcile_all()

        assert service.stats["reconciliations"] == 0
        assert not service._predictions

    frame = pd.DataFrame(
        {
            "Open": [100.0],
            "High": [101.0],
            "Low": [99.0],
            "Close": [100.0],
            "Volume": [10],
            "Amount": [1000.0],
        },
        index=pd.DatetimeIndex(["2026-07-13 09:00:00"], name="Time"),
    )

    async def update(*args, **kwargs):
        holder["service"]._engine = PredictingEngine()
        return frame

    monkeypatch.setattr(live_module, "update_cache", update)
    asyncio.run(scenario())
