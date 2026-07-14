import pandas as pd
from fastapi import APIRouter, HTTPException, Query

from pivot.config import Timeframe
from pivot.ingestion.cache import cache_path, load_cache_window
from pivot.ingestion.fetch import Region, cache_broker, filter_overseas_day_market
from pivot.ingestion.indicators import DEFAULT_WINDOWS, add_moving_averages
from server.deps import DATA_ROOT
from server.serialize import (
    US_EASTERN,
    chart_payload,
    display_frame,
    display_time_value,
    market_time,
)

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


def _parse_range(value: str | None, timeframe: Timeframe) -> pd.Timestamp | None:
    if not value:
        return None
    try:
        if timeframe.type != "day" and value.isdigit():
            return pd.to_datetime(int(value), unit="s")
        return pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, f"invalid chart range value: {value!r}") from exc


@router.get("/{symbol}")
def chart(
    symbol: str,
    timeframe: str = "day",
    ma: str | None = Query(None, description="comma-separated moving average windows"),
    limit: int | None = Query(None, ge=100, le=MAX_CHART_LIMIT),
    before: str | None = Query(
        None, description="exclusive upper bound: yyyy-mm-dd or unix seconds"
    ),
    start: str | None = Query(None, description="inclusive display range start"),
    end: str | None = Query(None, description="inclusive display range end"),
    region: Region = "domestic",
    exchange: str = "",
) -> dict:
    tf = Timeframe.from_code(timeframe)
    ma_windows = _parse_ma_windows(ma)
    chart_limit = _default_limit(tf, limit)
    lookback = max(ma_windows, default=1) - 1
    source_timezone = US_EASTERN if region == "overseas" else None
    range_start = market_time(_parse_range(start, tf), tf, source_timezone)
    range_end = market_time(_parse_range(end, tf), tf, source_timezone)
    if range_start is not None and range_end is not None and range_start > range_end:
        raise HTTPException(422, "start must be on or before end")
    range_before = (
        range_end + pd.Timedelta(nanoseconds=1) if range_end is not None else None
    )
    df, has_more = load_cache_window(
        cache_path(DATA_ROOT, cache_broker(region, exchange), tf.code, symbol),
        before=(
            range_before
            if range_before is not None
            else market_time(_parse_before(before, tf), tf, source_timezone)
        ),
        limit=chart_limit,
        lookback=lookback,
        columns=["Open", "High", "Low", "Close", "Volume"],
    )
    if df is None or df.empty:
        raise HTTPException(
            404, f"no cached data for {symbol} ({tf.code}) — run ingest first"
        )

    df = filter_overseas_day_market(df, tf, region)
    if df.empty:
        raise HTTPException(404, "no candles outside the overseas day market")
    df = add_moving_averages(df, windows=ma_windows)
    if range_start is not None:
        df = df.loc[df.index >= range_start]
    if range_end is not None:
        df = df.loc[df.index <= range_end]
    if df.empty:
        raise HTTPException(404, "no candles in requested chart range")
    if chart_limit is not None:
        df = df.tail(chart_limit)
    if start is not None or end is not None:
        has_more = False
    displayed = display_frame(df, tf, source_timezone)
    return {
        "symbol": symbol,
        "timeframe": tf.code,
        "has_more": has_more,
        "next_before": (
            None
            if not has_more
            else str(display_time_value(df.index[0], tf, source_timezone))
        ),
        **chart_payload(displayed, tf, ma_windows),
    }
