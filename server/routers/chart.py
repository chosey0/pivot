from fastapi import APIRouter, HTTPException, Query

from pivot.config import Timeframe
from pivot.ingestion.cache import cache_path, load_cache
from pivot.ingestion.fetch import BROKER
from pivot.ingestion.indicators import DEFAULT_WINDOWS, add_moving_averages
from server.deps import DATA_ROOT
from server.serialize import chart_payload

router = APIRouter(prefix="/api/chart", tags=["chart"])


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
    return {
        "symbol": symbol,
        "timeframe": tf.code,
        **chart_payload(df, tf, ma_windows),
    }
