import asyncio
import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from brokers.kiwoom._internal.http import HttpResponse
from brokers.kiwoom.endpoints.registry import EndpointSpec

from pivot.config import Timeframe
from pivot.ingestion.fetch import (
    OVERSEAS_CHART_PATH,
    _OverseasRestAdapter,
    _normalize_overseas_time,
    cache_broker,
    fetch_bars,
    update_cache,
)
from pivot.ingestion.cache import cache_path
from server.routers.ingest import IngestRequest, _warmup_start
from server.serialize import US_EASTERN, market_time
from server.routers.watchlist import WatchItem


class ChartSpy:
    def __init__(self):
        self.calls = []

    async def daily(self, symbol, **kwargs):
        self.calls.append(("daily", symbol, kwargs))
        return []

    async def minute(self, symbol, **kwargs):
        self.calls.append(("minute", symbol, kwargs))
        return []

    async def tick(self, symbol, **kwargs):
        self.calls.append(("tick", symbol, kwargs))
        return []


@pytest.mark.parametrize(
    ("code", "method", "unit_key", "unit"),
    [
        ("day", "daily", None, None),
        ("min5", "minute", "interval_minutes", 5),
        ("tick30", "tick", "tick_scope", 30),
    ],
)
def test_overseas_fetch_dispatches_to_kiwoom_chart(code, method, unit_key, unit):
    chart = ChartSpy()
    client = SimpleNamespace(overseas=SimpleNamespace(chart=chart))

    asyncio.run(
        fetch_bars(
            client,
            "AAPL",
            Timeframe.from_code(code),
            start_date="2026-07-01 000000",
            end_date=datetime.date(2026, 7, 14),
            region="overseas",
            exchange="ND",
        )
    )

    called_method, symbol, kwargs = chart.calls[0]
    assert (called_method, symbol, kwargs["exchange"]) == (method, "AAPL", "ND")
    if method != "tick":
        assert kwargs["start_date"] == "2026-07-14"
    if unit_key:
        assert kwargs[unit_key] == unit


def test_overseas_cache_and_watch_item_require_exchange():
    assert cache_broker() == "kiwoom"
    assert cache_broker("overseas", "ny") == "kiwoom-overseas-ny"
    assert WatchItem(symbol="005930").region == "domestic"
    assert WatchItem(symbol="aapl", region="overseas", exchange="nd").symbol == "AAPL"
    with pytest.raises(ValueError, match="overseas exchange"):
        WatchItem(symbol="AAPL", region="overseas")


def test_overseas_adapter_corrects_only_the_sdk_path():
    class RequestSpy:
        def __init__(self):
            self.spec = None

        async def request_raw(self, spec, *args, **kwargs):
            self.spec = spec
            return HttpResponse(
                payload={
                    "result_list": [
                        {"dt": "20260702"},
                        {"dt": "20260701"},
                        {"dt": "20260630"},
                    ]
                },
                headers={"cont-yn": "Y", "next-key": "page-2"},
                status_code=200,
            )

    client = RequestSpy()
    original = EndpointSpec("overseas.chart.daily", "POST", "/wrong", "usa06012")

    result = asyncio.run(
        _OverseasRestAdapter(client, "2026-07-01").request_raw(original)
    )

    assert original.path == "/wrong"
    assert client.spec.path == OVERSEAS_CHART_PATH
    assert [row["dt"] for row in result.payload["result_list"]] == [
        "20260702",
        "20260701",
    ]
    assert result.headers["cont-yn"] == "N"


def test_overseas_extended_hour_rolls_into_the_next_calendar_day():
    row = _normalize_overseas_time({"cntr_tm": "20260710274300"})

    assert row["cntr_tm"] == "20260711034300"
    assert row["cntr_tm_original"] == "20260710274300"


def test_overseas_daily_refresh_backfills_a_partial_cache(tmp_path, monkeypatch):
    path = cache_path(tmp_path, "kiwoom-overseas-nd", "day", "AAPL")
    path.parent.mkdir(parents=True)
    pd.DataFrame(
        {
            "Open": [200.0],
            "High": [201.0],
            "Low": [199.0],
            "Close": [200.5],
            "Volume": [100],
            "Amount": [20050.0],
        },
        index=pd.DatetimeIndex(["2026-07-01"], name="Time"),
    ).to_parquet(path)
    calls = []

    async def fake_fetch_bars(*args, **kwargs):
        calls.append(kwargs)
        return [
            SimpleNamespace(
                timestamp="20200102",
                open=100,
                high=101,
                low=99,
                close=100.5,
                volume=200,
                amount=20100,
            )
        ]

    monkeypatch.setattr("pivot.ingestion.fetch.fetch_bars", fake_fetch_bars)
    frame = asyncio.run(
        update_cache(
            object(),
            "AAPL",
            Timeframe.from_code("day"),
            tmp_path,
            region="overseas",
            exchange="ND",
        )
    )

    assert calls[0]["start_date"] is None
    assert list(frame.index) == [pd.Timestamp("2020-01-02"), pd.Timestamp("2026-07-01")]


def test_intraday_cache_uses_second_precision_boundaries(tmp_path, monkeypatch):
    calls = []

    async def fake_fetch_bars(*args, **kwargs):
        calls.append(kwargs)
        return [
            SimpleNamespace(
                timestamp=f"20260714090{minute}00",
                open=100,
                high=101,
                low=99,
                close=100.5,
                volume=200,
                amount=20100,
            )
            for minute in range(3)
        ]

    monkeypatch.setattr("pivot.ingestion.fetch.fetch_bars", fake_fetch_bars)
    frame = asyncio.run(
        update_cache(
            object(),
            "005930",
            Timeframe.from_code("min1"),
            tmp_path,
            start=datetime.datetime(2026, 7, 14, 9, 0, 30),
            end=datetime.datetime(2026, 7, 14, 9, 1, 30),
        )
    )

    assert calls[0]["start_date"] == "2026-07-14 090030"
    assert calls[0]["end_date"] == datetime.date(2026, 7, 14)
    assert list(frame.index) == [pd.Timestamp("2026-07-14 09:01:00")]


def test_ingest_request_accepts_intraday_boundaries():
    request = IngestRequest(
        symbols=["005930"],
        timeframe="min1",
        start="2026-07-14T09:00:01",
        end="2026-07-14T09:01:02",
    )

    assert request.start == datetime.datetime(2026, 7, 14, 9, 0, 1)
    assert request.end == datetime.datetime(2026, 7, 14, 9, 1, 2)


def test_overseas_collection_boundaries_convert_from_kst_to_market_time():
    timeframe = Timeframe.from_code("min1")

    assert market_time(
        pd.Timestamp("2026-07-14 22:30:00"), timeframe, US_EASTERN
    ) == pd.Timestamp("2026-07-14 09:30:00")


def test_overseas_daily_collection_date_maps_to_kst_close_date():
    assert market_time(
        pd.Timestamp("2026-07-15"), Timeframe.from_code("day"), US_EASTERN
    ) == pd.Timestamp("2026-07-14")


@pytest.mark.parametrize(
    ("timeframe", "expected"),
    [
        ("min1", datetime.datetime(2026, 7, 14, 7, 0)),
        ("min5", datetime.datetime(2026, 7, 13, 23, 0)),
    ],
)
def test_minute_collection_includes_120_bar_ma_warmup(timeframe, expected):
    assert _warmup_start(
        datetime.datetime(2026, 7, 14, 9, 0),
        Timeframe.from_code(timeframe),
    ) == expected


def test_day_and_tick_collection_do_not_add_minute_warmup():
    start = datetime.datetime(2026, 7, 14, 9, 0)

    assert _warmup_start(start, Timeframe.from_code("day")) == start
    assert _warmup_start(start, Timeframe.from_code("tick30")) == start
