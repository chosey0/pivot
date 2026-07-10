import pandas as pd

from pivot.ingestion.cache import load_cache_window


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
