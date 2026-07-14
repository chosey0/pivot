import pandas as pd
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from pivot.ingestion.cache import cache_path, cache_status, load_cache_window
from server.routers import chart as chart_api
from server.routers import ingest as ingest_api


def make_cache(path):
    index = pd.date_range("2026-01-01 09:00", periods=10, freq="min", name="Time")
    df = pd.DataFrame(
        {
            "Open": range(10),
            "High": range(1, 11),
            "Low": range(-1, 9),
            "Close": range(10),
            "Volume": range(100, 110),
            "Amount": range(1_000, 1_010),
        },
        index=index,
    )
    df.to_parquet(path)
    return df


def test_load_cache_window_limits_to_recent_rows_with_lookback(tmp_path):
    path = tmp_path / "cache.parquet"
    source = make_cache(path)

    window, has_more = load_cache_window(path, limit=3, lookback=2, columns=["Close"])

    assert has_more
    assert list(window.index) == list(source.index[-5:])
    assert list(window.columns) == ["Close"]


def test_load_cache_window_before_is_exclusive(tmp_path):
    path = tmp_path / "cache.parquet"
    source = make_cache(path)
    before = source.index[7]

    window, has_more = load_cache_window(path, before=before, limit=3, columns=["Close"])

    assert has_more
    assert list(window.index) == list(source.index[4:7])
    assert window.index[-1] < before


def test_load_cache_window_reports_no_more_when_window_reaches_start(tmp_path):
    path = tmp_path / "cache.parquet"
    source = make_cache(path)

    window, has_more = load_cache_window(path, limit=20, columns=["Close"])

    assert not has_more
    assert list(window.index) == list(source.index)


def test_cache_status_can_describe_one_collection_range(tmp_path):
    path = tmp_path / "cache.parquet"
    source = make_cache(path)

    status = cache_status(path, start=source.index[2], end=source.index[5])

    assert status is not None
    assert status["bars"] == 4
    assert status["first"] == source.index[2].isoformat()
    assert status["last"] == source.index[5].isoformat()


def test_ingest_status_accepts_collection_range_query(tmp_path, monkeypatch):
    path = cache_path(tmp_path, "kiwoom", "min1", "005930")
    path.parent.mkdir(parents=True)
    make_cache(path)
    monkeypatch.setattr(ingest_api, "DATA_ROOT", tmp_path)
    app = FastAPI()
    app.include_router(ingest_api.router)

    response = TestClient(app).get(
        "/api/ingest/status",
        params={
            "symbols": "005930",
            "timeframe": "min1",
            "start": "2026-01-01T09:02:00",
            "end": "2026-01-01T09:05:00",
        },
    )

    assert response.status_code == 200
    assert response.json()["005930"]["bars"] == 4


def test_chart_range_hides_ma_warmup_bars(tmp_path, monkeypatch):
    path = cache_path(tmp_path, "kiwoom", "min1", "005930")
    path.parent.mkdir(parents=True)
    index = pd.date_range("2026-07-14 07:00", periods=126, freq="min", name="Time")
    pd.DataFrame(
        {
            "Open": range(126),
            "High": range(1, 127),
            "Low": range(-1, 125),
            "Close": range(126),
            "Volume": 100,
        },
        index=index,
    ).to_parquet(path)
    monkeypatch.setattr(chart_api, "DATA_ROOT", tmp_path)

    response = chart_api.chart(
        "005930",
        "min1",
        "120",
        500,
        None,
        "2026-07-14T09:00:00",
        "2026-07-14T09:05:00",
        "domestic",
        "",
    )

    assert [point["time"] for point in response["candles"]] == [
        int(value.timestamp()) for value in index[-6:]
    ]
    assert response["ma"]["120"][0]["time"] == int(index[-6].timestamp())
    assert response["has_more"] is False


def test_chart_range_without_candles_returns_404(tmp_path, monkeypatch):
    path = cache_path(tmp_path, "kiwoom", "min1", "005930")
    path.parent.mkdir(parents=True)
    make_cache(path)
    monkeypatch.setattr(chart_api, "DATA_ROOT", tmp_path)

    with pytest.raises(HTTPException) as raised:
        chart_api.chart(
            "005930",
            "min1",
            "120",
            500,
            None,
            "2026-07-15T09:00:00",
            "2026-07-15T09:05:00",
            "domestic",
            "",
        )

    assert raised.value.status_code == 404
    assert raised.value.detail == "no candles in requested chart range"
