import json

import pytest
from fastapi import HTTPException

from pivot.ingestion.cache import cache_path
from server.routers import watchlist
from server.routers.watchlist import WatchItem, WatchItemUpdate


def test_legacy_item_expands_to_cached_timeframes(tmp_path, monkeypatch):
    path = tmp_path / "watchlist.json"
    path.write_text(json.dumps([{"symbol": "005930", "name": "삼성전자"}]), encoding="utf-8")
    data_root = tmp_path / "data"
    for timeframe in ("day", "min1", "tick30"):
        cache_path(data_root, "kiwoom", timeframe, "005930").mkdir(parents=True)
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


def test_removing_last_cache_reference_deletes_local_data(tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    monkeypatch.setattr(watchlist, "WATCHLIST_PATH", tmp_path / "watchlist.json")
    monkeypatch.setattr(watchlist, "DATA_ROOT", data_root)
    item = WatchItem(symbol="005930", timeframe="min1")
    cache = cache_path(data_root, "kiwoom", "min1", "005930")
    cache.mkdir(parents=True)
    watchlist.add_watch_item(item)

    watchlist.remove_watch_item("005930", timeframe="min1")

    assert not cache.exists()


def test_collection_range_can_be_updated_without_deleting_shared_cache(
    tmp_path, monkeypatch
):
    data_root = tmp_path / "data"
    monkeypatch.setattr(watchlist, "WATCHLIST_PATH", tmp_path / "watchlist.json")
    monkeypatch.setattr(watchlist, "DATA_ROOT", data_root)
    original = WatchItem(symbol="005930", timeframe="min1")
    replacement = WatchItem.model_validate(
        {
            **original.model_dump(),
            "start": "2026-07-15T09:00:00",
            "end": "2026-07-15T10:00:00",
        }
    )
    cache = cache_path(data_root, "kiwoom", "min1", "005930")
    cache.mkdir(parents=True)
    watchlist.add_watch_item(original)

    items = watchlist.update_watch_item(
        WatchItemUpdate(original=original, replacement=replacement)
    )

    assert items[0]["start"] == "2026-07-15T09:00:00"
    assert items[0]["end"] == "2026-07-15T10:00:00"
    assert cache.exists()


def test_removing_shared_range_keeps_cache_until_last_reference(tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    monkeypatch.setattr(watchlist, "WATCHLIST_PATH", tmp_path / "watchlist.json")
    monkeypatch.setattr(watchlist, "DATA_ROOT", data_root)
    first = WatchItem(
        symbol="AAPL",
        region="overseas",
        exchange="ND",
        timeframe="min1",
        start="2026-07-14T09:00:00",
        end="2026-07-14T10:00:00",
    )
    second = WatchItem(
        symbol="AAPL",
        region="overseas",
        exchange="ND",
        timeframe="min1",
        start="2026-07-14T10:00:00",
        end="2026-07-14T11:00:00",
    )
    cache = cache_path(data_root, "kiwoom-overseas-nd", "min1", "AAPL")
    cache.mkdir(parents=True)
    watchlist.add_watch_item(first)
    watchlist.add_watch_item(second)

    watchlist.remove_watch_item(
        "AAPL",
        timeframe="min1",
        region="overseas",
        exchange="ND",
        start=first.start,
        end=first.end,
    )
    assert cache.exists()

    watchlist.remove_watch_item(
        "AAPL",
        timeframe="min1",
        region="overseas",
        exchange="ND",
        start=second.start,
        end=second.end,
    )
    assert not cache.exists()


def test_watch_item_rejects_invalid_range():
    with pytest.raises(ValueError, match="start must be on or before end"):
        WatchItem(symbol="005930", start="2026-07-15", end="2026-07-14")
