import math

import pandas as pd
from fastapi import APIRouter, HTTPException, Query

from pivot.config import Timeframe
from pivot.ingestion.cache import cache_path, load_cache
from pivot.ingestion.fetch import BROKER
from pivot.ingestion.indicators import DEFAULT_WINDOWS, add_moving_averages
from server.deps import DATA_ROOT

router = APIRouter(prefix="/api/chart", tags=["chart"])


def _time_value(ts: pd.Timestamp, timeframe: Timeframe) -> str | int:
    # lightweight-charts 규약: 일봉 'yyyy-mm-dd', 분/틱봉 unix 초 (docs/04 §5)
    if timeframe.type == "day":
        return ts.date().isoformat()
    return int(ts.timestamp())


def _parse_ma_windows(value: str | None) -> tuple[int, ...]:
    if not value:
        return DEFAULT_WINDOWS
    windows: list[int] = []
    for part in value.split(","):
        text = part.strip()
        if not text:
            continue
        if not text.isdigit():
            raise HTTPException(422, f"invalid moving average window: {text!r}")
        window = int(text)
        if window <= 0:
            raise HTTPException(422, "moving average windows must be positive")
        windows.append(window)
    return tuple(dict.fromkeys(windows)) or DEFAULT_WINDOWS


@router.get("/{symbol}")
def chart(
    symbol: str,
    timeframe: str = "day",
    ma: str | None = Query(None, description="comma-separated moving average windows"),
) -> dict:
    tf = Timeframe.from_code(timeframe)
    ma_windows = _parse_ma_windows(ma)
    df = load_cache(cache_path(DATA_ROOT, BROKER, tf.code, symbol))
    if df is None or df.empty:
        raise HTTPException(404, f"no cached data for {symbol} ({tf.code}) — run ingest first")

    df = add_moving_averages(df, windows=ma_windows)

    times = [_time_value(ts, tf) for ts in df.index]
    candles = [
        {"time": t, "open": o, "high": h, "low": low, "close": c}
        for t, o, h, low, c in zip(
            times, df["Open"], df["High"], df["Low"], df["Close"]
        )
    ]
    volumes = [
        {"time": t, "value": int(v)} for t, v in zip(times, df["Volume"])
    ]
    ma = {
        str(w): [
            {"time": t, "value": v}
            for t, v in zip(times, df[str(w)])
            if not math.isnan(v)
        ]
        for w in ma_windows
    }
    return {
        "symbol": symbol,
        "timeframe": tf.code,
        "candles": candles,
        "volumes": volumes,
        "ma": ma,
    }
