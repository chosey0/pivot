"""브로커 중립 실시간 체결을 일·분·틱봉으로 집계한다."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from pivot.config import Timeframe

KST = ZoneInfo("Asia/Seoul")
_RECENT_TRADE_KEYS = 10_000
TradeOrder = tuple[datetime, int]


@dataclass(frozen=True)
class RealtimeTrade:
    symbol: str
    exchange_ts: datetime
    received_at: datetime
    received_seq: int
    price: Decimal
    volume: int

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("symbol is required")
        if self.exchange_ts.tzinfo is None or self.received_at.tzinfo is None:
            raise ValueError("realtime timestamps must be timezone-aware")
        if self.received_seq < 0:
            raise ValueError("received_seq must be non-negative")
        if self.price <= 0:
            raise ValueError("price must be positive")
        object.__setattr__(self, "symbol", self.symbol.strip())
        object.__setattr__(self, "exchange_ts", self.exchange_ts.astimezone(KST))
        object.__setattr__(self, "received_at", self.received_at.astimezone(KST))
        object.__setattr__(self, "volume", abs(int(self.volume)))

    @property
    def order(self) -> TradeOrder:
        return self.received_at, self.received_seq


@dataclass(frozen=True)
class Candle:
    symbol: str
    timeframe: str
    sequence: int
    start_at: datetime
    end_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    amount: Decimal
    trade_count: int


@dataclass(frozen=True)
class CandleUpdated:
    candle: Candle


@dataclass(frozen=True)
class CandleClosed:
    candle: Candle


@dataclass
class AggregationStats:
    accepted: int = 0
    duplicates: int = 0
    late: int = 0


@dataclass
class _CandleState:
    symbol: str
    timeframe: str
    sequence: int
    start_at: datetime
    end_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    amount: Decimal
    trade_count: int
    first_order: TradeOrder
    last_order: TradeOrder
    fixed_start: bool

    @classmethod
    def create(
        cls,
        trade: RealtimeTrade,
        *,
        timeframe: str,
        sequence: int,
        start_at: datetime,
        fixed_start: bool,
    ) -> _CandleState:
        return cls(
            symbol=trade.symbol,
            timeframe=timeframe,
            sequence=sequence,
            start_at=start_at,
            end_at=trade.exchange_ts,
            open=trade.price,
            high=trade.price,
            low=trade.price,
            close=trade.price,
            volume=trade.volume,
            amount=trade.price * trade.volume,
            trade_count=1,
            first_order=trade.order,
            last_order=trade.order,
            fixed_start=fixed_start,
        )

    def add(self, trade: RealtimeTrade) -> None:
        self.high = max(self.high, trade.price)
        self.low = min(self.low, trade.price)
        self.volume += trade.volume
        self.amount += trade.price * trade.volume
        self.trade_count += 1
        if trade.order < self.first_order:
            self.first_order = trade.order
            self.open = trade.price
            if not self.fixed_start:
                self.start_at = trade.exchange_ts
        if trade.order >= self.last_order:
            self.last_order = trade.order
            self.end_at = trade.exchange_ts
            self.close = trade.price

    def snapshot(self) -> Candle:
        return Candle(
            symbol=self.symbol,
            timeframe=self.timeframe,
            sequence=self.sequence,
            start_at=self.start_at,
            end_at=self.end_at,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            amount=self.amount,
            trade_count=self.trade_count,
        )


class CandleAggregator:
    """단일 종목·타임프레임의 현재 봉과 마감 이벤트를 관리한다."""

    def __init__(self, symbol: str, timeframe: Timeframe) -> None:
        if not symbol.strip():
            raise ValueError("symbol is required")
        self.symbol = symbol.strip()
        self.timeframe = timeframe
        self.stats = AggregationStats()
        self._state: _CandleState | None = None
        self._last_closed_start: datetime | None = None
        self._last_closed_end: datetime | None = None
        self._next_sequence = 0
        self._seen: set[TradeOrder] = set()
        self._seen_order: deque[TradeOrder] = deque()

    @property
    def current(self) -> Candle | None:
        return self._state.snapshot() if self._state else None

    def ingest(self, trade: RealtimeTrade) -> tuple[CandleUpdated | CandleClosed, ...]:
        if trade.symbol != self.symbol:
            raise ValueError(f"trade symbol {trade.symbol!r} does not match {self.symbol!r}")
        if not self._remember(trade.order):
            self.stats.duplicates += 1
            return ()

        if self.timeframe.type == "tick":
            if (
                self._last_closed_end is not None
                and trade.exchange_ts < self._last_closed_end
            ):
                self.stats.late += 1
                return ()
            return self._ingest_tick(trade)

        bucket = self._bucket_start(trade.exchange_ts)
        if self._last_closed_start is not None and bucket <= self._last_closed_start:
            self.stats.late += 1
            return ()
        if self._state is None:
            self.stats.accepted += 1
            return (CandleUpdated(self._start(trade, bucket, fixed_start=True)),)
        if bucket < self._state.start_at:
            self.stats.late += 1
            return ()
        if bucket > self._state.start_at:
            closed = self._close_current()
            self.stats.accepted += 1
            updated = CandleUpdated(self._start(trade, bucket, fixed_start=True))
            return closed, updated

        self._state.add(trade)
        self.stats.accepted += 1
        return (CandleUpdated(self._state.snapshot()),)

    def close_day(self) -> CandleClosed | None:
        if self.timeframe.type != "day":
            raise ValueError("close_day is only valid for day timeframe")
        if self._state is None:
            return None
        return self._close_current()

    def _ingest_tick(
        self, trade: RealtimeTrade
    ) -> tuple[CandleUpdated | CandleClosed, ...]:
        if self._state is None:
            candle = self._start(trade, trade.exchange_ts, fixed_start=False)
        else:
            self._state.add(trade)
            candle = self._state.snapshot()
        self.stats.accepted += 1
        if candle.trade_count == self.timeframe.unit:
            return (self._close_current(),)
        return (CandleUpdated(candle),)

    def _start(
        self, trade: RealtimeTrade, start_at: datetime, *, fixed_start: bool
    ) -> Candle:
        self._state = _CandleState.create(
            trade,
            timeframe=self.timeframe.code,
            sequence=self._next_sequence,
            start_at=start_at,
            fixed_start=fixed_start,
        )
        self._next_sequence += 1
        return self._state.snapshot()

    def _close_current(self) -> CandleClosed:
        if self._state is None:
            raise RuntimeError("no candle to close")
        candle = self._state.snapshot()
        self._last_closed_start = candle.start_at
        self._last_closed_end = candle.end_at
        self._state = None
        return CandleClosed(candle)

    def _bucket_start(self, timestamp: datetime) -> datetime:
        local = timestamp.astimezone(KST)
        if self.timeframe.type == "day":
            return local.replace(hour=0, minute=0, second=0, microsecond=0)
        minute = local.minute - local.minute % self.timeframe.unit
        return local.replace(minute=minute, second=0, microsecond=0)

    def _remember(self, key: TradeOrder) -> bool:
        if key in self._seen:
            return False
        self._seen.add(key)
        self._seen_order.append(key)
        if len(self._seen_order) > _RECENT_TRADE_KEYS:
            self._seen.remove(self._seen_order.popleft())
        return True
