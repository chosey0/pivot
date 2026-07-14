import json

import pytest
from fastapi import HTTPException

from server.routers import watchlist
from server.routers.watchlist import WatchItem


def test_legacy_item_expands_to_cached_timeframes(tmp_path, monkeypatch):
    path = tmp_path / "watchlist.json"
    path.write_text(json.dumps([{"symbol": "005930", "name": "삼성전자"}]), encoding="utf-8")
    data_root = tmp_path / "data"
    for timeframe in ("day", "min1", "tick30"):
        cache = data_root / "raw" / "kiwoom" / timeframe / "005930.parquet"
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.touch()
    monkeypatch.setattr(watchlist, "WATCHLIST_PATH", path)
    monkeypatch.setattr(watchlist, "DATA_ROOT", data_root)

    assert [item["timeframe"] for item in watchlist.list_watchlist()] == [
        "day",
        "min1",
        "tick30",
    ]


def test_same_symbol_can_have_distinct_timeframe_and_range_items(tmp_path, monkeypatch):
    monkeypatch.setattr(watchlist, "WATCHLIST_PATH", tmp_path / "watchlist.json")
    day = WatchItem(symbol="005930", timeframe="day")
    minute_a = WatchItem(
        symbol="005930",
        timeframe="min1",
        start="2026-07-14T09:00:00",
        end="2026-07-14T10:00:00",
    )
    minute_b = WatchItem(
        symbol="005930",
        timeframe="min1",
        start="2026-07-14T10:00:00",
        end="2026-07-14T10:00:00",
    )

    watchlist.add_watch_item(day)
    watchlist.add_watch_item(minute_a)
    items = watchlist.add_watch_item(minute_b)

    assert [(item["timeframe"], item["start"]) for item in items] == [
        ("day", None),
        ("min1", "2026-07-14T09:00:00"),
        ("min1", "2026-07-14T10:00:00"),
    ]
    with pytest.raises(HTTPException, match="same data item already exists"):
        watchlist.add_watch_item(minute_a)

    remaining = watchlist.remove_watch_item(
        "005930",
        timeframe="min1",
        start="2026-07-14T09:00:00",
        end="2026-07-14T10:00:00",
    )
    assert [(item["timeframe"], item["start"]) for item in remaining] == [
        ("day", None),
        ("min1", "2026-07-14T10:00:00"),
    ]


def test_watch_item_rejects_invalid_range():
    with pytest.raises(ValueError, match="start must be on or before end"):
        WatchItem(symbol="005930", start="2026-07-15", end="2026-07-14")
