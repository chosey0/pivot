import pandas as pd
from fastapi import APIRouter, HTTPException, Query

from pivot.config import Timeframe
from pivot.ingestion.cache import cache_path, load_cache_window
from pivot.ingestion.fetch import Region, cache_broker
from pivot.ingestion.indicators import DEFAULT_WINDOWS, add_moving_averages
from server.deps import DATA_ROOT
from server.serialize import chart_payload

router = APIRouter(prefix="/api/chart", tags=["chart"])

DEFAULT_INTRADAY_LIMIT = 5_000
MAX_CHART_LIMIT = 20_000


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


def _default_limit(timeframe: Timeframe, limit: int | None) -> int | None:
    if limit is not None:
        return limit
    if timeframe.type in {"minute", "tick"}:
        return DEFAULT_INTRADAY_LIMIT
    return None


def _parse_before(value: str | None, timeframe: Timeframe) -> pd.Timestamp | None:
    if not value:
        return None
    try:
        if timeframe.type == "day":
            return pd.Timestamp(value)
        return pd.to_datetime(int(value), unit="s")
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, f"invalid before value: {value!r}") from exc


@router.get("/{symbol}")
def chart(
    symbol: str,
    timeframe: str = "day",
    ma: str | None = Query(None, description="comma-separated moving average windows"),
    limit: int | None = Query(None, ge=100, le=MAX_CHART_LIMIT),
    before: str | None = Query(None, description="exclusive upper bound: yyyy-mm-dd or unix seconds"),
    region: Region = "domestic",
    exchange: str = "",
) -> dict:
    tf = Timeframe.from_code(timeframe)
    ma_windows = _parse_ma_windows(ma)
    chart_limit = _default_limit(tf, limit)
    lookback = max(ma_windows, default=1) - 1
    df, has_more = load_cache_window(
        cache_path(DATA_ROOT, cache_broker(region, exchange), tf.code, symbol),
        before=_parse_before(before, tf),
        limit=chart_limit,
        lookback=lookback,
        columns=["Open", "High", "Low", "Close", "Volume"],
    )
    if df is None or df.empty:
        raise HTTPException(404, f"no cached data for {symbol} ({tf.code}) — run ingest first")

    df = add_moving_averages(df, windows=ma_windows)
    if chart_limit is not None:
        df = df.tail(chart_limit)
    return {
        "symbol": symbol,
        "timeframe": tf.code,
        "has_more": has_more,
        "next_before": None if not has_more else df.index[0].isoformat(),
        **chart_payload(df, tf, ma_windows),
    }
