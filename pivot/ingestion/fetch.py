"""broker-modules(Kiwoom) 캔들 조회 + 캐시 갱신. docs/03 §2·§6.

SDK 시그니처 (brokers.kiwoom.domestic.chart.DomesticChartAPI):
- daily(symbol, *, base_date, adjusted=True, max_pages=None, start_date=None)
- minute(symbol, *, interval_minutes=1, base_date=None, adjusted=True, max_pages=None, start_date=None)
- tick(symbol, *, tick_scope=1, adjusted=True, max_pages=None, start_date=None)
rate limit(1700) 재시도는 SDK가 내장. 반환은 timestamp 오름차순 list[ChartBar].
"""

import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from pivot.config import Timeframe
from pivot.ingestion.cache import cache_path, load_cache, merge_cache
from pivot.ingestion.schema import bars_to_frame

if TYPE_CHECKING:
    from brokers.kiwoom import KiwoomClient
    from brokers.kiwoom.models.ohlcv import ChartBar

BROKER = "kiwoom"


async def fetch_bars(
    client: "KiwoomClient",
    symbol: str,
    timeframe: Timeframe,
    *,
    start_date: str | None = None,
    end_date: datetime.date | None = None,
    max_pages: int | None = None,
) -> list["ChartBar"]:
    base_date = end_date or datetime.date.today()
    if timeframe.type == "day":
        return await client.domestic.chart.daily(
            symbol, base_date=base_date, max_pages=max_pages, start_date=start_date
        )
    if timeframe.type == "minute":
        return await client.domestic.chart.minute(
            symbol,
            interval_minutes=timeframe.unit,
            base_date=base_date,
            max_pages=max_pages,
            start_date=start_date,
        )
    return await client.domestic.chart.tick(
        symbol, tick_scope=timeframe.unit, max_pages=max_pages, start_date=start_date
    )


async def update_cache(
    client: "KiwoomClient",
    symbol: str,
    timeframe: Timeframe,
    data_root: Path,
    *,
    start: datetime.date | None = None,
    end: datetime.date | None = None,
    max_pages: int | None = None,
) -> pd.DataFrame:
    """캐시 갱신.

    기간이 명시되면 해당 구간을 조회해 캐시에 병합한다. 기간이 없으면 기존 캐시의
    마지막 봉 이후만 증분 조회하고, 캐시가 없으면 가능한 전체 구간을 조회한다.
    """
    if start and end and start > end:
        raise ValueError("start date must be on or before end date")

    path = cache_path(data_root, BROKER, timeframe.code, symbol)
    existing = load_cache(path)

    start_date = None
    if start is not None:
        if timeframe.type == "day":
            start_date = start.isoformat()
        else:
            start_date = f"{start.isoformat()} 000000"
    elif existing is not None and not existing.empty:
        last: pd.Timestamp = existing.index[-1]
        if timeframe.type == "day":
            start_date = last.date().isoformat()
        else:
            # tick/minute의 start_date 포맷: "YYYY-MM-DD HHMMSS"
            start_date = last.strftime("%Y-%m-%d %H%M%S")

    bars = await fetch_bars(
        client,
        symbol,
        timeframe,
        start_date=start_date,
        end_date=end,
        max_pages=max_pages,
    )
    frame = bars_to_frame(bars)
    if end is not None and not frame.empty:
        end_ts = pd.Timestamp(datetime.datetime.combine(end, datetime.time.max))
        frame = frame.loc[frame.index <= end_ts]
    if frame.empty:
        return existing if existing is not None else frame
    return merge_cache(path, frame)
