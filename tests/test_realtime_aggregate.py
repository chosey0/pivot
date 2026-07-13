"""M5 broker-neutral 실시간 봉 집계 계약."""

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from pivot.config import Timeframe
from pivot.realtime.aggregate import (
    CandleAggregator,
    CandleClosed,
    CandleUpdated,
    RealtimeTrade,
)

KST = ZoneInfo("Asia/Seoul")


def trade(
    time: str,
    price: str,
    volume: int = 1,
    *,
    seq: int,
    received_at: str | None = None,
) -> RealtimeTrade:
    exchange_ts = datetime.fromisoformat(f"2026-07-13T{time}").replace(tzinfo=KST)
    received_ts = datetime.fromisoformat(
        f"2026-07-13T{received_at or time}"
    ).replace(tzinfo=KST)
    return RealtimeTrade(
        symbol="005930",
        exchange_ts=exchange_ts,
        received_at=received_ts,
        received_seq=seq,
        price=Decimal(price),
        volume=volume,
    )


def test_minute_aggregator_closes_on_next_boundary():
    aggregator = CandleAggregator("005930", Timeframe(type="minute", unit=3))

    assert isinstance(aggregator.ingest(trade("09:00:10", "100", 2, seq=1))[0], CandleUpdated)
    assert isinstance(aggregator.ingest(trade("09:02:59", "103", 3, seq=2))[0], CandleUpdated)
    events = aggregator.ingest(trade("09:03:00", "101", 4, seq=3))

    assert [type(event) for event in events] == [CandleClosed, CandleUpdated]
    closed = events[0].candle
    assert closed.start_at == datetime(2026, 7, 13, 9, 0, tzinfo=KST)
    assert (closed.open, closed.high, closed.low, closed.close) == (
        Decimal("100"),
        Decimal("103"),
        Decimal("100"),
        Decimal("103"),
    )
    assert closed.volume == 5
    assert closed.amount == Decimal("509")
    assert closed.trade_count == 2
    assert events[1].candle.sequence == closed.sequence + 1


def test_same_bucket_order_is_deterministic_and_duplicate_is_ignored():
    aggregator = CandleAggregator("005930", Timeframe(type="minute", unit=1))
    later = trade("09:00:20", "102", 2, seq=2, received_at="09:00:20")
    earlier = trade("09:00:10", "100", 1, seq=1, received_at="09:00:10")

    aggregator.ingest(later)
    current = aggregator.ingest(earlier)[0].candle

    assert (current.open, current.close) == (Decimal("100"), Decimal("102"))
    assert current.volume == 3
    assert aggregator.ingest(earlier) == ()
    assert aggregator.stats.duplicates == 1


def test_trade_for_closed_minute_is_counted_as_late_without_rewind():
    aggregator = CandleAggregator("005930", Timeframe(type="minute", unit=1))
    aggregator.ingest(trade("09:00:10", "100", seq=1))
    aggregator.ingest(trade("09:01:00", "101", seq=2))

    assert aggregator.ingest(trade("09:00:50", "99", seq=3)) == ()
    assert aggregator.current is not None
    assert aggregator.current.close == Decimal("101")
    assert aggregator.stats.late == 1


def test_tick_aggregator_closes_exactly_on_nth_trade():
    aggregator = CandleAggregator("005930", Timeframe(type="tick", unit=3))

    assert isinstance(aggregator.ingest(trade("09:00:00", "100", seq=1))[0], CandleUpdated)
    assert isinstance(aggregator.ingest(trade("09:00:00", "102", seq=2))[0], CandleUpdated)
    events = aggregator.ingest(trade("09:00:00", "99", seq=3))

    assert len(events) == 1
    assert isinstance(events[0], CandleClosed)
    assert events[0].candle.trade_count == 3
    assert events[0].candle.sequence == 0
    next_bar = aggregator.ingest(trade("09:00:00", "101", seq=4))[0].candle
    assert next_bar.sequence == 1


def test_tick_aggregator_rejects_older_trade_but_allows_same_second():
    aggregator = CandleAggregator("005930", Timeframe(type="tick", unit=3))
    aggregator.ingest(trade("09:00:01", "100", seq=1))
    aggregator.ingest(trade("09:00:02", "101", seq=2))
    aggregator.ingest(trade("09:00:02", "102", seq=3))

    assert aggregator.ingest(trade("09:00:01", "99", seq=4)) == ()
    assert aggregator.stats.late == 1
    same_second = aggregator.ingest(trade("09:00:02", "103", seq=5))
    assert isinstance(same_second[0], CandleUpdated)


def test_day_aggregator_closes_explicitly_and_on_date_change():
    aggregator = CandleAggregator("005930", Timeframe(type="day"))
    aggregator.ingest(trade("09:00:00", "100", seq=1))

    closed = aggregator.close_day()
    assert isinstance(closed, CandleClosed)
    assert aggregator.close_day() is None

    next_day = RealtimeTrade(
        symbol="005930",
        exchange_ts=datetime(2026, 7, 14, 9, 0, tzinfo=KST),
        received_at=datetime(2026, 7, 14, 9, 0, tzinfo=KST),
        received_seq=1,
        price=Decimal("101"),
        volume=1,
    )
    assert isinstance(aggregator.ingest(next_day)[0], CandleUpdated)


def test_date_change_closes_previous_day_before_new_update():
    aggregator = CandleAggregator("005930", Timeframe(type="day"))
    aggregator.ingest(trade("15:29:59", "100", seq=1))
    next_day = RealtimeTrade(
        symbol="005930",
        exchange_ts=datetime(2026, 7, 14, 9, 0, tzinfo=KST),
        received_at=datetime(2026, 7, 14, 9, 0, tzinfo=KST),
        received_seq=1,
        price=Decimal("101"),
        volume=1,
    )

    events = aggregator.ingest(next_day)
    assert [type(event) for event in events] == [CandleClosed, CandleUpdated]
    assert events[0].candle.start_at.date().isoformat() == "2026-07-13"
    assert events[1].candle.start_at.date().isoformat() == "2026-07-14"


def test_trade_normalizes_timezone_and_signed_volume():
    utc = ZoneInfo("UTC")
    item = RealtimeTrade(
        symbol="005930",
        exchange_ts=datetime(2026, 7, 13, 0, 0, tzinfo=utc),
        received_at=datetime(2026, 7, 13, 0, 0, tzinfo=utc),
        received_seq=1,
        price=Decimal("100"),
        volume=-3,
    )

    assert item.exchange_ts == datetime(2026, 7, 13, 9, 0, tzinfo=KST)
    assert item.volume == 3
