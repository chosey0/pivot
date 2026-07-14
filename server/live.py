"""M5 Kiwoom realtime session, candle aggregation, inference, and fan-out."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import re
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd
import torch

from pivot.config import Timeframe
from pivot.dataset.build import run_preprocess
from pivot.ingestion.cache import cache_path, load_cache_window
from pivot.ingestion.fetch import (
    OVERSEAS_EXCHANGES,
    Region,
    cache_broker,
    fetch_bars,
    filter_overseas_day_market,
    is_supported_overseas_time,
    update_cache,
)
from pivot.ingestion.indicators import add_moving_averages
from pivot.ingestion.schema import bars_to_frame
from pivot.realtime.aggregate import (
    Candle,
    CandleAggregator,
    CandleClosed,
    CandleUpdated,
)
from pivot.realtime.aggregate import KST, RealtimeTrade
from pivot.realtime.infer import LiveInferenceEngine, LivePrediction, LiveWarmupError
from pivot.storage.deployments import DeploymentRepository
from pivot.storage.runs import RunRepository
from pivot.storage.supabase import StorageObjectClient
from pivot.training.checkpoint import load_verified_checkpoint
from server.serialize import (
    US_EASTERN,
    chart_payload,
    display_frame,
    display_time_value,
    market_time,
    time_value,
)

LIVE_HISTORY_LIMIT = 5_000
RECENT_PREDICTIONS = 200
CLIENT_QUEUE_SIZE = 256
LIVE_DISPLAY_TIMEFRAMES = ("day", "min1")
LIVE_MINUTE_PAGE_DAYS = 7
LIVE_DAY_PAGE_DAYS = 365
DEFAULT_PREDICTION_THRESHOLD = 0.7
OVERSEAS_SYMBOL_RE = re.compile(r"^[A-Z0-9.-]{1,20}$")
logger = logging.getLogger(__name__)


def _now(timezone: ZoneInfo) -> dt.datetime:
    return dt.datetime.now(timezone)


class LiveServiceError(RuntimeError):
    pass


class ListenerClosed(RuntimeError):
    pass


@dataclass(frozen=True)
class LiveTarget:
    symbol: str
    name: str = ""
    region: Region = "domestic"
    exchange: str = ""

    def __post_init__(self) -> None:
        symbol = str(self.symbol).strip().upper()
        name = str(self.name).strip()
        region = str(self.region).strip().lower()
        exchange = str(self.exchange).strip().upper()
        if region not in {"domestic", "overseas"}:
            raise ValueError("region must be domestic or overseas")
        if region == "domestic":
            symbol = _symbol(symbol, "domestic")
            exchange = ""
        else:
            symbol = _symbol(symbol, "overseas")
            if exchange not in OVERSEAS_EXCHANGES:
                raise ValueError("overseas exchange must be one of: NA, ND, NY")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "region", region)
        object.__setattr__(self, "exchange", exchange)

    @property
    def market(self) -> str:
        return "US" if self.region == "overseas" else "KRX"

    @property
    def timezone(self) -> ZoneInfo:
        return US_EASTERN if self.region == "overseas" else KST

    def payload(self) -> dict[str, str]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "region": self.region,
            "exchange": self.exchange,
        }


class SubscriptionStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, LiveTarget]:
        if not self.path.exists():
            return {}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("live subscriptions must be a JSON list")
        targets = [
            LiveTarget(symbol=value) if isinstance(value, str) else LiveTarget(**value)
            for value in payload
        ]
        # ponytail: symbol keys assume one primary US listing; use composite IDs if dual listings appear.
        return {target.symbol: target for target in targets}

    def save(self, targets: dict[str, LiveTarget]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                [targets[symbol].payload() for symbol in sorted(targets)],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(self.path)


@dataclass(eq=False)
class LiveListener:
    maxsize: int = CLIENT_QUEUE_SIZE
    events: deque[dict] = field(default_factory=deque)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    closed: bool = False

    async def get(self) -> dict:
        async with self.condition:
            await self.condition.wait_for(lambda: self.events or self.closed)
            if not self.events:
                raise ListenerClosed("live listener was closed")
            return self.events.popleft()

    async def put(self, event: dict) -> bool:
        async with self.condition:
            if self.closed:
                return False
            event_type = event["type"]
            if event_type == "candle_update":
                symbol = event["data"]["symbol"]
                timeframe = event["data"].get("timeframe")
                for index in range(len(self.events) - 1, -1, -1):
                    current = self.events[index]
                    if (
                        current["type"] == "candle_closed"
                        and current["data"]["symbol"] == symbol
                        and current["data"].get("timeframe") == timeframe
                    ):
                        break
                    if (
                        current["type"] == "candle_update"
                        and current["data"]["symbol"] == symbol
                        and current["data"].get("timeframe") == timeframe
                    ):
                        self.events[index] = event
                        self.condition.notify()
                        return True
            if len(self.events) >= self.maxsize:
                removable = next(
                    (
                        index
                        for index, current in enumerate(self.events)
                        if current["type"] in {"candle_update", "heartbeat"}
                    ),
                    None,
                )
                if removable is not None:
                    del self.events[removable]
                elif event_type in {"candle_update", "heartbeat"}:
                    return False
                else:
                    self.closed = True
                    self.condition.notify_all()
                    return False
            self.events.append(event)
            self.condition.notify()
            return True

    async def close(self) -> None:
        async with self.condition:
            self.closed = True
            self.condition.notify_all()


class LiveService:
    def __init__(
        self,
        data_root: Path,
        *,
        deployments: DeploymentRepository | None = None,
        runs: RunRepository | None = None,
        storage: StorageObjectClient | None = None,
        client_factory: Callable[[], Any] | None = None,
        subscription_path: Path | None = None,
        reconcile_interval: float = 300.0,
        stale_after: float = 60.0,
        heartbeat_interval: float = 15.0,
    ) -> None:
        self.data_root = data_root
        self.subscription_store = SubscriptionStore(
            subscription_path or data_root / "meta" / "live_subscriptions.json"
        )
        self.deployments = deployments
        self.runs = runs
        self.storage = storage
        self.client_factory = client_factory
        self.reconcile_interval = reconcile_interval
        self.stale_after = stale_after
        self.heartbeat_interval = heartbeat_interval

        self._desired: dict[str, LiveTarget] = {}
        self._subscription_state: dict[str, dict] = {}
        self._aggregators: dict[tuple[str, str], CandleAggregator] = {}
        self._closed_overlay: dict[tuple[str, str], deque[Candle]] = {}
        self._latest_candles: dict[tuple[str, str], dict] = {}
        self._predictions: deque[dict] = deque(maxlen=RECENT_PREDICTIONS)
        self._listeners: set[LiveListener] = set()
        self._engine: LiveInferenceEngine | None = None
        self._deployment: dict | None = None
        self._prediction_threshold = DEFAULT_PREDICTION_THRESHOLD
        self._client: Any = None
        self._sessions: dict[str, Any] = {}
        self._gateway_task: asyncio.Task | None = None
        self._bootstrap_task: asyncio.Task | None = None
        self._maintenance_task: asyncio.Task | None = None
        self._started = False
        self._closed = False
        self._sequence = 0
        self._connection = "closed"
        self._connection_message = ""
        self._last_tick_at: dt.datetime | None = None
        self._last_tick_by_symbol: dict[str, dt.datetime] = {}
        self._last_heartbeat_at: dt.datetime | None = None
        self._market_state = "closed"
        self._last_reconcile_at: dt.datetime | None = None
        self._lock = asyncio.Lock()
        self._rest_lock = asyncio.Lock()
        self._activation_lock = asyncio.Lock()
        self._inference_lock = asyncio.Lock()
        self.stats = {
            "invalid_events": 0,
            "reconciliations": 0,
            "reconcile_errors": 0,
            "inference_errors": 0,
        }

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        try:
            self._desired = self.subscription_store.load()
        except Exception as exc:
            logger.error("cannot load live subscriptions (%s)", type(exc).__name__)
            self._connection_message = "cannot load saved subscriptions"
        self._sync_subscription_state()
        self._bootstrap_task = asyncio.create_task(self._bootstrap())
        self._maintenance_task = asyncio.create_task(self._maintenance_loop())
        await self._ensure_gateway()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        for listener in list(self._listeners):
            await listener.close()

        for session in list(self._sessions.values()):
            try:
                await session.close()
            except Exception as exc:
                logger.warning(
                    "Kiwoom realtime session close failed (%s)", type(exc).__name__
                )

        tasks = [
            task
            for task in (
                self._gateway_task,
                self._bootstrap_task,
                self._maintenance_task,
            )
            if task is not None
        ]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._gateway_task = None
        self._bootstrap_task = None
        self._maintenance_task = None
        self._sessions.clear()
        self._client = None
        self._connection = "closed"

    async def activate_model(self, run_id: int, artifact_id: int | None = None) -> dict:
        async with self._activation_lock:
            deployments, runs, storage = self._repositories()
            run = await asyncio.to_thread(runs.get, run_id)
            if run["status"] != "succeeded":
                raise LiveServiceError(f"run {run_id} is not succeeded")
            artifact = await asyncio.to_thread(
                runs.best_artifact if artifact_id is None else runs.artifact,
                run_id,
                *(() if artifact_id is None else (artifact_id,)),
            )
            checkpoint = await self._load_checkpoint(storage, run, artifact)
            candidate = LiveInferenceEngine(
                checkpoint, deployment_id=0, device=torch.device("cpu")
            )
            await asyncio.to_thread(candidate.warmup)
            deployment = await asyncio.to_thread(
                deployments.activate,
                run_id=run_id,
                artifact_id=artifact["id"],
            )
            engine = LiveInferenceEngine(
                checkpoint,
                deployment_id=deployment["id"],
                device=torch.device("cpu"),
            )
            engine.set_prediction_threshold(self._prediction_threshold)
            await self._install_engine(
                engine,
                self._public_deployment(
                    deployment, run, timeframe=engine.preset.timeframe.code
                ),
            )
            await self._reconcile_all()
            await self._broadcast_snapshot()
            return self.state()

    async def set_prediction_threshold(self, threshold: float) -> dict:
        if not 0 <= threshold <= 1:
            raise ValueError("prediction threshold must be between 0 and 1")
        async with self._activation_lock:
            async with self._inference_lock:
                self._prediction_threshold = threshold
                if self._engine is not None:
                    self._engine.set_prediction_threshold(threshold)
        await self._broadcast_snapshot()
        return self.state()

    async def set_manual_anchor(
        self, symbol: str, timeframe: Timeframe, time: pd.Timestamp
    ) -> dict:
        symbol = str(symbol).strip().upper()
        async with self._activation_lock:
            engine = self._engine
            target = self._desired.get(symbol)
            if engine is None:
                raise LiveServiceError("no active live model")
            if target is None:
                raise KeyError(symbol)
            if timeframe.code != engine.preset.timeframe.code:
                raise ValueError("manual anchor timeframe must match the active model")
            source_timezone = target.timezone if target.region == "overseas" else None
            anchor_time = market_time(time, timeframe, source_timezone)
            if anchor_time is None:
                raise ValueError("manual anchor time is required")
            frame = await asyncio.to_thread(self._history, symbol, timeframe)
            async with self._inference_lock:
                await asyncio.to_thread(
                    engine.set_manual_anchor, symbol, anchor_time, frame
                )
        await self._broadcast_snapshot()
        return self.state()

    async def clear_manual_anchor(self, symbol: str) -> dict:
        symbol = str(symbol).strip().upper()
        async with self._activation_lock:
            if symbol not in self._desired:
                raise KeyError(symbol)
            async with self._inference_lock:
                if self._engine is not None:
                    self._engine.clear_manual_anchor(symbol)
        await self._broadcast_snapshot()
        return self.state()

    async def deactivate_model(self) -> dict:
        async with self._activation_lock:
            deployments, _, _ = self._repositories()
            await asyncio.to_thread(deployments.deactivate)
            async with self._lock:
                self._engine = None
                self._deployment = None
                self._aggregators.clear()
                observed_at = dt.datetime.now(dt.UTC)
                for symbol in self._desired:
                    self._reset_aggregators(symbol, observed_at)
                self._predictions.clear()
                self._closed_overlay.clear()
                self._latest_candles.clear()
                self._sync_subscription_state()
            await self._broadcast_snapshot()
            return self.state()

    async def subscribe(
        self,
        symbol: str,
        *,
        name: str = "",
        region: Region = "domestic",
        exchange: str = "",
    ) -> dict:
        target = LiveTarget(symbol=symbol, name=name, region=region, exchange=exchange)
        symbol = target.symbol
        async with self._lock:
            if symbol in self._desired:
                return self._subscription_state[symbol].copy()
            self._desired[symbol] = target
            self.subscription_store.save(self._desired)
            self._subscription_state[symbol] = self._new_subscription_state(symbol)
            session = self._sessions.get(target.market)
        if session is not None:
            try:
                await self._subscribe_session(session, target)
                self._set_subscription_status(symbol, "subscribed")
                self._reset_aggregators(symbol, dt.datetime.now(dt.UTC))
            except Exception as exc:
                logger.error(
                    "cannot subscribe live symbol %s (%s)",
                    symbol,
                    type(exc).__name__,
                )
                self._set_subscription_status(symbol, "error", "subscription failed")
                raise LiveServiceError(f"cannot subscribe {symbol}") from exc
        elif self._gateway_task is not None and not self._gateway_task.done():
            await self._restart_gateway()
        else:
            await self._ensure_gateway()
        if self._client is not None and self._engine is not None:
            if not await self._reconcile_symbol(symbol, self._engine):
                self._last_reconcile_at = None
        await self._broadcast("subscription", self._subscription_event(symbol))
        return self._subscription_state[symbol].copy()

    async def unsubscribe(self, symbol: str) -> None:
        symbol = str(symbol).strip().upper()
        async with self._lock:
            if symbol not in self._desired:
                raise KeyError(symbol)
            target = self._desired[symbol]
            session = self._sessions.get(target.market)
        if session is not None:
            try:
                await self._unsubscribe_session(session, target)
            except Exception as exc:
                logger.error(
                    "cannot unsubscribe live symbol %s (%s)",
                    symbol,
                    type(exc).__name__,
                )
                self._set_subscription_status(symbol, "error", "unsubscribe failed")
                raise LiveServiceError(f"cannot unsubscribe {symbol}") from exc
        async with self._lock:
            self._desired.pop(symbol)
            self.subscription_store.save(self._desired)
            self._subscription_state.pop(symbol, None)
            for mapping in (
                self._aggregators,
                self._closed_overlay,
                self._latest_candles,
            ):
                for key in [key for key in mapping if key[0] == symbol]:
                    mapping.pop(key, None)
        async with self._inference_lock:
            if self._engine is not None:
                self._engine.clear_manual_anchor(symbol)
        await self._broadcast_snapshot()
        if not self._desired and self._gateway_task is not None:
            self._gateway_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._gateway_task
            await self._set_connection("closed", "")
        elif target.market not in {item.market for item in self._desired.values()}:
            await self._restart_gateway()

    async def chart_history(
        self,
        symbol: str,
        timeframe: Timeframe,
        ma_windows: tuple[int, ...],
        *,
        before: pd.Timestamp | None = None,
    ) -> dict:
        symbol = str(symbol).strip().upper()
        if timeframe.code not in LIVE_DISPLAY_TIMEFRAMES:
            raise ValueError("live chart timeframe must be day or min1")
        if symbol not in self._desired:
            raise ValueError(f"{symbol} is not subscribed")
        target = self._desired[symbol]
        source_timezone = target.timezone if target.region == "overseas" else None
        market_before = market_time(before, timeframe, source_timezone)
        market_now = _now(target.timezone).replace(tzinfo=None)

        end = (
            market_before - pd.Timedelta(microseconds=1)
            if market_before is not None
            else pd.Timestamp(market_now)
        )
        if timeframe.type == "minute" and before is None:
            display_start = end.normalize()
        else:
            page_days = (
                LIVE_MINUTE_PAGE_DAYS
                if timeframe.type == "minute"
                else LIVE_DAY_PAGE_DAYS
            )
            display_start = end.normalize() - pd.Timedelta(days=page_days - 1)

        engine = self._engine
        preset = (
            engine.preset
            if engine is not None and engine.preset.timeframe.code == timeframe.code
            else None
        )
        training_windows = (
            [preset.fractal.n, *preset.required_ma_windows]
            if preset is not None
            else []
        )
        max_window = max([*ma_windows, *training_windows], default=1)
        lookback_days = (
            max(7, ((max_window + 389) // 390) * 7)
            if timeframe.type == "minute"
            else max(180, max_window * 2)
        )
        fetch_start = display_start - pd.Timedelta(days=lookback_days)
        start_date = fetch_start.date().isoformat()
        if timeframe.type == "minute":
            start_date = f"{start_date} 000000"

        future_bars = (preset.fractal.n - 1) // 2 if preset is not None else 0
        future_days = (
            max(1, ((future_bars + 389) // 390) * 7)
            if timeframe.type == "minute"
            else future_bars * 2
        )
        fetch_end = min(
            end.normalize() + pd.Timedelta(days=future_days),
            pd.Timestamp(market_now),
        )

        async def fetch(client) -> list:
            return await fetch_bars(
                client,
                symbol,
                timeframe,
                start_date=start_date,
                end_date=fetch_end.date(),
                region=target.region,
                exchange=target.exchange,
            )

        if self._client is not None:
            async with self._rest_lock:
                bars = await fetch(self._client)
        else:
            factory = self.client_factory or _kiwoom_client
            async with factory() as client:
                bars = await fetch(client)

        source = filter_overseas_day_market(
            bars_to_frame(bars), timeframe, target.region
        )
        if timeframe.type == "day" and before is None:
            self._seed_current_day(symbol, source)
        frame = add_moving_averages(source, windows=ma_windows)
        if frame.empty:
            page = frame
            has_more = False
        else:
            page = frame.loc[(frame.index >= display_start) & (frame.index <= end)]
            has_more = bool((frame.index < display_start).any())
        fractal_markers = []
        if preset is not None and not source.empty:
            result = await asyncio.to_thread(run_preprocess, source, preset)
            visible_times = set(page.index)
            fractal_markers = [
                {
                    "time": display_time_value(
                        pd.Timestamp(time), timeframe, source_timezone
                    ),
                    "kind": str(row.kind),
                    "label": int(row.label),
                }
                for time, row in result.points.iterrows()
                if time in visible_times
            ]
        display_page = display_frame(page, timeframe, source_timezone)
        return {
            "symbol": symbol,
            "timeframe": timeframe.code,
            "has_more": has_more,
            "next_before": (
                None
                if page.empty or not has_more
                else str(
                    display_time_value(
                        pd.Timestamp(page.index[0]), timeframe, source_timezone
                    )
                )
            ),
            "fractal_markers": fractal_markers,
            **chart_payload(display_page, timeframe, ma_windows),
        }

    def state(self) -> dict:
        aggregator_stats = {}
        for symbol in self._desired:
            aggregator = self._aggregators.get((symbol, "min1"))
            if aggregator is None:
                continue
            aggregator_stats[symbol] = {
                "accepted": aggregator.stats.accepted,
                "duplicates": aggregator.stats.duplicates,
                "late": aggregator.stats.late,
            }
        counters = {
            **self.stats,
            "accepted": sum(item["accepted"] for item in aggregator_stats.values()),
            "duplicates": sum(item["duplicates"] for item in aggregator_stats.values()),
            "late": sum(item["late"] for item in aggregator_stats.values()),
        }
        return {
            "connection": {
                "status": self._connection,
                "message": self._connection_message or None,
                "last_tick_at": _iso(self._last_tick_at),
                "last_heartbeat_at": _iso(self._last_heartbeat_at),
                "market_state": self._market_state,
            },
            "deployment": self._deployment.copy() if self._deployment else None,
            "prediction_threshold": self._prediction_threshold,
            "manual_anchors": self._manual_anchor_state(),
            "subscriptions": self.subscriptions(),
            "counters": counters,
        }

    def snapshot(self) -> dict:
        candles = []
        for symbol in sorted(self._desired):
            keys = sorted(
                {
                    key
                    for mapping in (self._closed_overlay, self._latest_candles)
                    for key in mapping
                    if key[0] == symbol
                }
            )
            for key in keys:
                group = [
                    self._candle_payload(candle, provisional=False)
                    for candle in self._closed_overlay.get(key, ())
                ]
                latest = self._latest_candles.get(key)
                if latest is not None and (
                    not group
                    or group[-1]["candle"]["time"] != latest["candle"]["time"]
                    or group[-1]["provisional"] != latest["provisional"]
                ):
                    group.append(latest)
                candles.extend(group)
        return {
            **self.state(),
            "latest_candles": candles,
            "recent_predictions": list(self._predictions),
        }

    def subscriptions(self) -> list[dict]:
        return [
            self._subscription_state[symbol].copy() for symbol in sorted(self._desired)
        ]

    def _manual_anchor_state(self) -> list[dict]:
        if self._engine is None:
            return []
        timeframe = self._engine.preset.timeframe
        rows = []
        for symbol, time in sorted(self._engine.manual_anchors().items()):
            target = self._desired.get(symbol)
            if target is None:
                continue
            source_timezone = target.timezone if target.region == "overseas" else None
            rows.append(
                {
                    "symbol": symbol,
                    "timeframe": timeframe.code,
                    "time": display_time_value(time, timeframe, source_timezone),
                }
            )
        return rows

    async def add_listener(self, *, maxsize: int = CLIENT_QUEUE_SIZE) -> LiveListener:
        listener = LiveListener(maxsize=maxsize)
        listener.events.append(self._envelope("snapshot", self.snapshot()))
        self._listeners.add(listener)
        return listener

    async def remove_listener(self, listener: LiveListener) -> None:
        self._listeners.discard(listener)
        await listener.close()

    async def handle_sdk_event(self, event: Any) -> None:
        tr_id = getattr(event, "tr_id", None)
        if tr_id not in {"0B", "FE"}:
            return
        symbol = str(getattr(event, "symbol", "")).strip().upper()
        target = self._desired.get(symbol)
        if target is None or tr_id != ("FE" if target.region == "overseas" else "0B"):
            return
        try:
            trade = trade_from_tick(event, target)
        except (TypeError, ValueError):
            self.stats["invalid_events"] += 1
            return
        if target.region == "overseas" and not is_supported_overseas_time(
            trade.exchange_ts
        ):
            return
        self._last_tick_at = dt.datetime.now(dt.UTC)
        self._last_tick_by_symbol[trade.symbol] = self._last_tick_at
        if trade.symbol not in self._desired:
            return
        if self._connection == "stale":
            await self._reconcile_all()
            await self._set_connection("connected", "")
        self._set_subscription_status(trade.symbol, "subscribed")
        self._subscription_state[trade.symbol]["last_tick_at"] = _iso(
            self._last_tick_at
        )
        engine = self._engine
        if engine is None:
            self._set_inference_status(trade.symbol, "no_model")
        timeframe_codes = set(LIVE_DISPLAY_TIMEFRAMES)
        if engine is not None:
            timeframe_codes.add(engine.preset.timeframe.code)
        for code in sorted(timeframe_codes):
            key = (trade.symbol, code)
            aggregator = self._aggregators.setdefault(
                key,
                CandleAggregator(
                    trade.symbol,
                    Timeframe.from_code(code),
                    observing_since=trade.received_at,
                    timezone=target.timezone,
                ),
            )
            for result in aggregator.ingest(trade):
                if engine is not None and engine is not self._engine:
                    return
                if result.candle.partial_from_subscription:
                    continue
                if isinstance(result, CandleUpdated):
                    payload = self._candle_payload(result.candle, provisional=True)
                    self._latest_candles[key] = payload
                    await self._broadcast("candle_update", payload)
                elif isinstance(result, CandleClosed):
                    inference_engine = (
                        engine
                        if engine is not None and code == engine.preset.timeframe.code
                        else None
                    )
                    await self._handle_closed(
                        result.candle,
                        engine=inference_engine,
                    )

    async def _bootstrap(self) -> None:
        try:
            deployments, runs, storage = self._repositories()
            deployment = await asyncio.to_thread(deployments.active)
            if deployment is None:
                return
            run = await asyncio.to_thread(runs.get, deployment["run_id"])
            artifact = await asyncio.to_thread(
                runs.artifact, run["id"], deployment["artifact_id"]
            )
            checkpoint = await self._load_checkpoint(storage, run, artifact)
            engine = LiveInferenceEngine(
                checkpoint,
                deployment_id=deployment["id"],
                device=torch.device("cpu"),
            )
            engine.set_prediction_threshold(self._prediction_threshold)
            await self._install_engine(
                engine,
                self._public_deployment(
                    deployment, run, timeframe=engine.preset.timeframe.code
                ),
            )
            await self._reconcile_all()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("cannot restore active live model (%s)", type(exc).__name__)
            self._connection_message = "active model could not be restored"

    def _repositories(
        self,
    ) -> tuple[DeploymentRepository, RunRepository, StorageObjectClient]:
        if self.deployments is None or self.runs is None or self.storage is None:
            from server.deps import deployment_repo, object_storage, run_repo

            self.deployments = self.deployments or deployment_repo()
            self.runs = self.runs or run_repo()
            self.storage = self.storage or object_storage()
        return self.deployments, self.runs, self.storage

    async def _load_checkpoint(self, storage, run: dict, artifact: dict):
        data = await asyncio.to_thread(
            storage.download, artifact["bucket"], artifact["object_path"]
        )
        return await asyncio.to_thread(
            load_verified_checkpoint,
            data,
            artifact["sha256"],
            expected_config=run["config"],
        )

    async def _install_engine(
        self, engine: LiveInferenceEngine, deployment: dict
    ) -> None:
        async with self._lock:
            self._engine = engine
            self._deployment = deployment
            self._aggregators.clear()
            observed_at = dt.datetime.now(dt.UTC)
            for symbol in self._desired:
                self._reset_aggregators(symbol, observed_at)
            self._closed_overlay.clear()
            self._latest_candles.clear()
            self._sync_subscription_state()

    def _reset_aggregators(self, symbol: str, observed_at: dt.datetime) -> None:
        target = self._desired[symbol]
        codes = set(LIVE_DISPLAY_TIMEFRAMES)
        if self._engine is not None:
            codes.add(self._engine.preset.timeframe.code)
        for code in codes:
            key = (symbol, code)
            self._aggregators[key] = CandleAggregator(
                symbol,
                Timeframe.from_code(code),
                observing_since=observed_at,
                timezone=target.timezone,
            )
            self._latest_candles.pop(key, None)

    def _seed_current_day(self, symbol: str, frame: pd.DataFrame) -> None:
        target = self._desired[symbol]
        now = dt.datetime.now(target.timezone)
        today = pd.Timestamp(now.replace(tzinfo=None)).normalize()
        if today not in frame.index:
            return
        row = frame.loc[today]
        close = Decimal(str(row["Close"]))
        volume = int(row["Volume"])
        amount = (
            close * volume if pd.isna(row["Amount"]) else Decimal(str(row["Amount"]))
        )
        key = (symbol, "day")
        aggregator = self._aggregators.setdefault(
            key,
            CandleAggregator(
                symbol,
                Timeframe.from_code("day"),
                observing_since=now,
                timezone=target.timezone,
            ),
        )
        seeded = aggregator.seed_current(
            Candle(
                symbol=symbol,
                timeframe="day",
                sequence=0,
                start_at=now.replace(hour=0, minute=0, second=0, microsecond=0),
                end_at=now,
                open=Decimal(str(row["Open"])),
                high=Decimal(str(row["High"])),
                low=Decimal(str(row["Low"])),
                close=close,
                volume=volume,
                amount=amount,
                trade_count=0,
            )
        )
        if seeded is not None:
            self._latest_candles[key] = self._candle_payload(seeded, provisional=True)

    async def _ensure_gateway(self) -> None:
        if not self._desired or self._closed:
            return
        if self._gateway_task is None or self._gateway_task.done():
            self._gateway_task = asyncio.create_task(self._gateway_loop())

    async def _restart_gateway(self) -> None:
        task = self._gateway_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._gateway_task = None
        await self._ensure_gateway()

    @staticmethod
    async def _subscribe_session(session: Any, target: LiveTarget) -> None:
        if target.region == "overseas":
            await session.subscribe_us_trades(target.symbol, exchange=target.exchange)
        else:
            await session.subscribe_trades(target.symbol)

    @staticmethod
    async def _unsubscribe_session(session: Any, target: LiveTarget) -> None:
        if target.region == "overseas":
            await session.unsubscribe(
                target.symbol,
                channel="us_trades",
                market="US",
                exchange=target.exchange,
            )
        else:
            await session.unsubscribe(target.symbol)

    @staticmethod
    async def _forward_session(session: Any, queue: asyncio.Queue) -> None:
        try:
            async for event in session.stream():
                await queue.put(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await queue.put(exc)

    async def _gateway_loop(self) -> None:
        while self._desired and not self._closed:
            await self._set_connection("connecting", "Kiwoom WebSocket connecting")
            try:
                factory = self.client_factory or _kiwoom_client
                async with factory() as client:
                    self._client = client
                    markets = sorted(
                        {target.market for target in self._desired.values()}
                    )
                    async with contextlib.AsyncExitStack() as stack:
                        for market in markets:
                            self._sessions[market] = await stack.enter_async_context(
                                client.realtime.session(market=market)
                            )
                        for symbol in sorted(self._desired):
                            target = self._desired[symbol]
                            await self._subscribe_session(
                                self._sessions[target.market], target
                            )
                            self._reset_aggregators(symbol, dt.datetime.now(dt.UTC))
                            self._set_subscription_status(symbol, "subscribed")
                            await self._broadcast(
                                "subscription",
                                self._subscription_event(symbol),
                            )
                        await self._set_connection("connected", "")
                        await self._reconcile_all()
                        queue: asyncio.Queue = asyncio.Queue()
                        stream_tasks = [
                            asyncio.create_task(self._forward_session(session, queue))
                            for session in self._sessions.values()
                        ]
                        try:
                            while self._desired and not self._closed:
                                event = await queue.get()
                                if isinstance(event, Exception):
                                    raise event
                                await self.handle_sdk_event(event)
                        finally:
                            for task in stream_tasks:
                                task.cancel()
                            await asyncio.gather(*stream_tasks, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._closed:
                    break
                logger.error("Kiwoom realtime gateway failed (%s)", type(exc).__name__)
                await self._set_connection(
                    "reconnecting", "Kiwoom realtime connection failed"
                )
                await asyncio.sleep(1)
            finally:
                self._sessions.clear()
                self._client = None
        await self._set_connection("closed", "")

    async def _maintenance_loop(self) -> None:
        last_heartbeat = 0.0
        while not self._closed:
            await asyncio.sleep(min(self.heartbeat_interval, 5.0))
            now = dt.datetime.now(dt.UTC)
            monotonic = asyncio.get_running_loop().time()
            if monotonic - last_heartbeat >= self.heartbeat_interval:
                self._last_heartbeat_at = now
                self._market_state = _market_state(now, self._desired.values())
                await self._broadcast(
                    "heartbeat",
                    {
                        "server_time": now.isoformat(),
                        "market_state": self._market_state,
                        "last_tick_at": _iso(self._last_tick_at),
                    },
                )
                last_heartbeat = monotonic
            if self._last_tick_at is not None:
                idle = (now - self._last_tick_at).total_seconds()
                if (
                    idle >= self.stale_after
                    and self._connection == "connected"
                    and self._market_state == "open"
                ):
                    await self._set_connection("stale", "no recent trade events")
            due = (
                self._last_reconcile_at is None
                or (now - self._last_reconcile_at).total_seconds()
                >= self.reconcile_interval
            )
            if due and self._client is not None and self._engine is not None:
                await self._reconcile_all()
            await self._close_day_if_due(now.astimezone(KST))

    async def _close_day_if_due(self, now: dt.datetime) -> None:
        for (symbol, code), aggregator in list(self._aggregators.items()):
            if code != "day":
                continue
            target = self._desired.get(symbol)
            if target is None:
                continue
            local_now = now.astimezone(target.timezone)
            close_time = (
                dt.time(16, 0) if target.region == "overseas" else dt.time(15, 30)
            )
            if local_now.time() < close_time:
                continue
            current = aggregator.current
            if current is None or current.start_at.date() != local_now.date():
                continue
            closed = aggregator.close_day()
            if closed is not None:
                engine = (
                    self._engine
                    if self._engine is not None and self._timeframe().code == code
                    else None
                )
                await self._handle_closed(closed.candle, engine=engine)

    async def _handle_closed(
        self, candle: Candle, *, engine: LiveInferenceEngine | None
    ) -> None:
        key = (candle.symbol, candle.timeframe)
        payload = self._candle_payload(candle, provisional=False)
        self._latest_candles[key] = payload
        self._closed_overlay.setdefault(key, deque(maxlen=LIVE_HISTORY_LIMIT)).append(
            candle
        )
        await self._broadcast("candle_closed", payload)
        if engine is not None:
            await self._infer(candle.symbol, engine=engine)

    async def _infer(
        self,
        symbol: str,
        frame: pd.DataFrame | None = None,
        *,
        engine: LiveInferenceEngine | None = None,
    ) -> None:
        active_engine = engine or self._engine
        if active_engine is None or symbol not in self._desired:
            return
        try:
            history = (
                frame
                if frame is not None
                else await asyncio.to_thread(
                    self._history, symbol, active_engine.preset.timeframe
                )
            )
            async with self._inference_lock:
                prediction = await asyncio.to_thread(
                    active_engine.infer, symbol, history
                )
            if (
                prediction is None
                or active_engine is not self._engine
                or symbol not in self._desired
            ):
                return
            payload = self._prediction_payload(prediction)
            self._predictions.append(payload)
            self._set_inference_status(symbol, "ready")
            await self._broadcast("prediction", payload)
        except LiveWarmupError as exc:
            if active_engine is not self._engine or symbol not in self._desired:
                return
            available = len(history) if "history" in locals() else 0
            self._set_inference_status(symbol, "warmup")
            await self._broadcast(
                "warmup",
                {
                    "symbol": symbol,
                    "required_bars": self._required_bars(active_engine),
                    "available_bars": available,
                    "reason": str(exc),
                },
            )
        except Exception as exc:
            if active_engine is not self._engine or symbol not in self._desired:
                return
            logger.error(
                "live inference failed for %s (%s)", symbol, type(exc).__name__
            )
            self.stats["inference_errors"] += 1
            await self._broadcast(
                "error",
                {
                    "scope": "inference",
                    "symbol": symbol,
                    "recoverable": True,
                    "message": "live inference failed",
                },
            )

    def _history(self, symbol: str, timeframe: Timeframe) -> pd.DataFrame:
        target = self._desired[symbol]
        frame, _ = load_cache_window(
            cache_path(
                self.data_root,
                cache_broker(target.region, target.exchange),
                timeframe.code,
                symbol,
            ),
            limit=LIVE_HISTORY_LIMIT,
            columns=["Open", "High", "Low", "Close", "Volume", "Amount"],
        )
        frames = [] if frame is None else [frame]
        overlays = self._closed_overlay.get((symbol, timeframe.code))
        if overlays:
            frames.append(_candles_frame(list(overlays)))
        if not frames:
            return pd.DataFrame(
                columns=["Open", "High", "Low", "Close", "Volume", "Amount"]
            ).rename_axis("Time")
        combined = pd.concat(frames)
        return (
            combined[~combined.index.duplicated(keep="last")]
            .sort_index()
            .tail(LIVE_HISTORY_LIMIT)
        )

    async def _reconcile_all(self) -> None:
        engine = self._engine
        if self._client is None or engine is None:
            return
        for symbol in sorted(self._desired):
            await self._reconcile_symbol(symbol, engine)
        self._last_reconcile_at = dt.datetime.now(dt.UTC)

    async def _reconcile_symbol(
        self, symbol: str, engine: LiveInferenceEngine
    ) -> bool:
        target = self._desired.get(symbol)
        if self._client is None or target is None or engine is not self._engine:
            return False
        try:
            async with self._rest_lock:
                frame = await update_cache(
                    self._client,
                    symbol,
                    engine.preset.timeframe,
                    self.data_root,
                    region=target.region,
                    exchange=target.exchange,
                )
            if symbol not in self._desired or engine is not self._engine:
                return False
            self.stats["reconciliations"] += 1
            await self._infer(
                symbol,
                frame.tail(LIVE_HISTORY_LIMIT),
                engine=engine,
            )
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if symbol not in self._desired or engine is not self._engine:
                return False
            logger.error(
                "candle reconciliation failed for %s (%s)",
                symbol,
                type(exc).__name__,
            )
            self.stats["reconcile_errors"] += 1
            await self._broadcast(
                "error",
                {
                    "scope": "reconcile",
                    "symbol": symbol,
                    "recoverable": True,
                    "message": "candle reconciliation failed",
                },
            )
            return False

    async def _set_connection(self, status: str, message: str) -> None:
        self._connection = status
        self._connection_message = message
        await self._broadcast("connection", {"status": status, "message": message})

    def _set_subscription_status(
        self, symbol: str, status: str, error: str | None = None
    ) -> None:
        state = self._subscription_state.setdefault(
            symbol, self._new_subscription_state(symbol)
        )
        state.update({"status": status, "error": error})

    def _sync_subscription_state(self) -> None:
        for symbol in self._desired:
            current = self._subscription_state.get(symbol, {})
            self._subscription_state[symbol] = {
                **self._new_subscription_state(symbol),
                **current,
                "inference_status": "warmup" if self._engine else "no_model",
            }

    def _new_subscription_state(self, symbol: str) -> dict:
        target = self._desired[symbol]
        return {
            "symbol": symbol,
            "name": target.name or None,
            "region": target.region,
            "exchange": target.exchange,
            "status": "pending",
            "inference_status": "warmup" if self._engine else "no_model",
            "error": None,
            "last_tick_at": _iso(self._last_tick_by_symbol.get(symbol)),
        }

    def _set_inference_status(self, symbol: str, status: str) -> None:
        state = self._subscription_state.setdefault(
            symbol, self._new_subscription_state(symbol)
        )
        state["inference_status"] = status
        state["last_tick_at"] = _iso(self._last_tick_by_symbol.get(symbol))

    def _subscription_event(self, symbol: str) -> dict:
        state = self._subscription_state[symbol]
        return {
            key: state[key]
            for key in (
                "symbol",
                "name",
                "region",
                "exchange",
                "status",
                "inference_status",
                "error",
                "last_tick_at",
            )
        }

    def _timeframe(self) -> Timeframe:
        if self._engine is None:
            raise LiveServiceError("no active live model")
        return self._engine.preset.timeframe

    @staticmethod
    def _required_bars(engine: LiveInferenceEngine) -> int:
        preset = engine.preset
        return max([preset.fractal.n, *preset.required_ma_windows], default=1)

    async def _broadcast(self, event_type: str, data: dict) -> None:
        event = self._envelope(event_type, data)
        failed = []
        for listener in tuple(self._listeners):
            if not await listener.put(event):
                if listener.closed:
                    failed.append(listener)
        for listener in failed:
            self._listeners.discard(listener)

    async def _broadcast_snapshot(self) -> None:
        await self._broadcast("snapshot", self.snapshot())

    def _envelope(self, event_type: str, data: dict) -> dict:
        self._sequence += 1
        return {
            "type": event_type,
            "sequence": self._sequence,
            "emitted_at": dt.datetime.now(dt.UTC).isoformat(),
            "data": data,
        }

    def _candle_payload(self, candle: Candle, *, provisional: bool) -> dict:
        timeframe = Timeframe.from_code(candle.timeframe)
        time = _live_time_value(candle.start_at, timeframe)
        return {
            "symbol": candle.symbol,
            "timeframe": candle.timeframe,
            "candle": {
                "time": time,
                "open": float(candle.open),
                "high": float(candle.high),
                "low": float(candle.low),
                "close": float(candle.close),
                "volume": candle.volume,
            },
            "provisional": provisional,
        }

    def _prediction_payload(self, prediction: LivePrediction) -> dict:
        timeframe = Timeframe.from_code(prediction.timeframe)
        target = self._desired.get(prediction.symbol)
        source_timezone = (
            target.timezone
            if target is not None and target.region == "overseas"
            else None
        )
        time = _live_time_value(
            prediction.closed_time, timeframe, source_timezone=source_timezone
        )
        return {
            "symbol": prediction.symbol,
            "timeframe": prediction.timeframe,
            "time": time,
            "scores": prediction.scores,
            "selected_class": prediction.selected_class,
            "candidate_windows": [
                {
                    "pairing_rule": item.candidate.pairing_rule,
                    "anchor_position": item.candidate.anchor_position,
                    "anchor_time": _live_time_value(
                        item.candidate.anchor_time,
                        timeframe,
                        source_timezone=source_timezone,
                    ),
                    "anchor_kind": item.candidate.anchor_kind,
                    "anchor_source": item.candidate.anchor_source,
                    "anchor_confidence": item.candidate.anchor_confidence,
                    "start": _live_time_value(
                        item.candidate.anchor_time,
                        timeframe,
                        source_timezone=source_timezone,
                    ),
                    "end": _live_time_value(
                        item.candidate.end_time,
                        timeframe,
                        source_timezone=source_timezone,
                    ),
                    "shared_window": item.candidate.shared_window,
                }
                for item in prediction.candidates
            ],
            "deployment_id": prediction.deployment_id,
        }

    @staticmethod
    def _public_deployment(deployment: dict, run: dict, *, timeframe: str) -> dict:
        dataset = run.get("dataset_snapshot", {}).get("dataset", {})
        return {
            "id": deployment["id"],
            "run_id": run["id"],
            "artifact_id": deployment["artifact_id"],
            "run_name": run["name"],
            "dataset_id": run["dataset_id"],
            "dataset_name": run.get("dataset_name", ""),
            "timeframe": timeframe,
            "feature_columns": dataset.get("feature_columns", []),
            "model": run.get("config", {}).get("model"),
            "pairing_rule": (
                dataset.get("preset_snapshot", {})
                .get("preset", {})
                .get("labeling", {})
                .get("sample_pairing", "latest_opposite_v1")
            ),
            "status": "active",
            "activated_at": deployment.get("activated_at"),
        }


def trade_from_tick(tick: Any, target: LiveTarget | None = None) -> RealtimeTrade:
    if tick.price is None or tick.volume is None:
        raise ValueError("trade has no price or volume")
    received = dt.datetime.fromisoformat(str(tick.received_at).replace("Z", "+00:00"))
    if received.tzinfo is None:
        raise ValueError("received_at must include a timezone")
    region: Region = (
        target.region
        if target is not None
        else "overseas"
        if getattr(tick, "tr_id", None) == "FE"
        else "domestic"
    )
    timezone = (
        target.timezone
        if target is not None
        else US_EASTERN
        if region == "overseas"
        else KST
    )
    try:
        exchange_time = dt.time.fromisoformat(str(tick.exchange_ts))
    except ValueError as exc:
        raise ValueError("invalid exchange timestamp") from exc
    exchange = dt.datetime.combine(
        received.astimezone(timezone).date(), exchange_time, tzinfo=timezone
    )
    return RealtimeTrade(
        symbol=_symbol(tick.symbol, region),
        exchange_ts=exchange,
        received_at=received,
        received_seq=int(tick.received_seq),
        price=Decimal(tick.price),
        volume=int(tick.volume),
        timezone=timezone,
    )


def _symbol(value: Any, region: Region = "domestic") -> str:
    symbol = str(value).strip().upper()
    if region == "domestic":
        if len(symbol) != 6 or not symbol.isdigit():
            raise ValueError("domestic symbol must contain six digits")
    elif not OVERSEAS_SYMBOL_RE.fullmatch(symbol):
        raise ValueError(
            "overseas symbol must contain 1-20 letters, digits, dots, or hyphens"
        )
    return symbol


def _live_time_value(
    value: dt.datetime | pd.Timestamp,
    timeframe: Timeframe,
    *,
    source_timezone: ZoneInfo | None = None,
) -> str | int:
    """실시간 aware 시각을 기존 차트의 KST 벽시계 인코딩으로 맞춘다."""
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is not None:
        source_timezone = timestamp.tzinfo
        timestamp = timestamp.tz_localize(None)
    if source_timezone is not None:
        return display_time_value(timestamp, timeframe, source_timezone)
    return time_value(timestamp, timeframe)


def _candles_frame(candles: list[Candle]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Time": candle.start_at.replace(tzinfo=None),
                "Open": float(candle.open),
                "High": float(candle.high),
                "Low": float(candle.low),
                "Close": float(candle.close),
                "Volume": candle.volume,
                "Amount": float(candle.amount),
            }
            for candle in candles
        ]
    ).set_index("Time")


def _market_state(now: dt.datetime, targets=()) -> str:
    markets = {(target.region, target.timezone) for target in targets} or {
        ("domestic", KST)
    }
    for region, timezone in markets:
        local = now.astimezone(timezone)
        if local.weekday() >= 5:
            continue
        opens = dt.time(9, 30) if region == "overseas" else dt.time(9, 0)
        closes = dt.time(16, 0) if region == "overseas" else dt.time(15, 30)
        if opens <= local.time() < closes:
            return "open"
    return "closed"


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value else None


def _kiwoom_client():
    from brokers.kiwoom import KiwoomClient

    return KiwoomClient.from_env()
