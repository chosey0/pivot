"""broker-modules(Kiwoom) 캔들 조회 + 캐시 갱신. docs/03 §2·§6.

SDK 시그니처 (brokers.kiwoom.domestic.chart.DomesticChartAPI):
- daily(symbol, *, base_date, adjusted=True, max_pages=None, start_date=None)
- minute(symbol, *, interval_minutes=1, base_date=None, adjusted=True, max_pages=None, start_date=None)
- tick(symbol, *, tick_scope=1, adjusted=True, max_pages=None, start_date=None)
rate limit(1700) 재시도는 SDK가 내장. 반환은 timestamp 오름차순 list[ChartBar].
"""

import datetime
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import pandas as pd

from pivot.config import Timeframe
from pivot.ingestion.cache import cache_path, load_cache, merge_cache
from pivot.ingestion.schema import bars_to_frame

if TYPE_CHECKING:
    from brokers.kiwoom import KiwoomClient
    from brokers.kiwoom.models.ohlcv import ChartBar

BROKER = "kiwoom"
Region = Literal["domestic", "overseas"]
OVERSEAS_EXCHANGES = {"NA", "ND", "NY"}
OVERSEAS_CHART_PATH = "/api/us/chart"


class _OverseasRestAdapter:
    """Remove after broker-modules ships the corrected US chart REST path."""

    def __init__(self, client: "KiwoomClient", start_date: str | None = None) -> None:
        self.client = client
        self.start = _boundary_timestamp(start_date) if start_date else None

    async def request_raw(self, spec: Any, *args: Any, **kwargs: Any) -> Any:
        response = await self.client.request_raw(
            replace(spec, path=OVERSEAS_CHART_PATH), *args, **kwargs
        )
        from brokers.kiwoom.parsers.rest import chart_rows, timestamp_value

        raw_rows = chart_rows(response.payload, "overseas")
        rows = [_normalize_overseas_time(row) for row in raw_rows]
        kept = (
            rows
            if self.start is None
            else [
                row
                for row in rows
                if pd.Timestamp(timestamp_value(row)) >= self.start
            ]
        )
        normalized = rows != raw_rows
        reached_start = len(kept) != len(rows)
        if not normalized and not reached_start:
            return response
        return replace(
            response,
            payload={**response.payload, "result_list": kept},
            headers=(
                {**response.headers, "cont-yn": "N", "next-key": ""}
                if reached_start
                else response.headers
            ),
        )


def _boundary_timestamp(value: str) -> pd.Timestamp:
    try:
        return pd.Timestamp(datetime.datetime.strptime(value, "%Y-%m-%d %H%M%S"))
    except ValueError:
        return pd.Timestamp(value)


def _normalize_overseas_time(row: dict[str, Any]) -> dict[str, Any]:
    value = str(row.get("cntr_tm") or "").strip()
    if len(value) not in {12, 14} or not value.isdigit() or int(value[8:10]) < 24:
        return row
    base = datetime.datetime.strptime(value[:8], "%Y%m%d")
    normalized = base + datetime.timedelta(
        hours=int(value[8:10]),
        minutes=int(value[10:12]),
        seconds=int(value[12:14] or "0"),
    )
    return {
        **row,
        "cntr_tm_original": value,
        "cntr_tm": normalized.strftime("%Y%m%d%H%M%S"),
    }


def _overseas_chart(client: "KiwoomClient", start_date: str | None) -> Any:
    if not hasattr(client, "request_raw"):
        return client.overseas.chart

    from brokers.kiwoom.overseas.chart import OverseasChartAPI

    return OverseasChartAPI(_OverseasRestAdapter(client, start_date))


def cache_broker(region: Region = "domestic", exchange: str = "") -> str:
    if region == "domestic":
        return BROKER
    normalized = exchange.strip().upper()
    if normalized not in OVERSEAS_EXCHANGES:
        raise ValueError("overseas exchange must be one of: NA, ND, NY")
    return f"{BROKER}-overseas-{normalized.lower()}"


async def fetch_bars(
    client: "KiwoomClient",
    symbol: str,
    timeframe: Timeframe,
    *,
    start_date: str | None = None,
    end_date: datetime.date | None = None,
    max_pages: int | None = None,
    region: Region = "domestic",
    exchange: str = "",
) -> list["ChartBar"]:
    if region == "overseas":
        normalized = exchange.strip().upper()
        cache_broker(region, normalized)
        query_date = end_date.isoformat() if end_date else None
        chart = _overseas_chart(client, start_date)
        if timeframe.type == "day":
            return await chart.daily(
                symbol,
                exchange=normalized,
                start_date=query_date,
                max_pages=max_pages,
            )
        if timeframe.type == "minute":
            return await chart.minute(
                symbol,
                exchange=normalized,
                start_date=query_date,
                interval_minutes=timeframe.unit,
                max_pages=max_pages,
            )
        return await chart.tick(
            symbol,
            exchange=normalized,
            tick_scope=timeframe.unit,
            max_pages=max_pages,
        )

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
    region: Region = "domestic",
    exchange: str = "",
) -> pd.DataFrame:
    """캐시 갱신.

    기간이 명시되면 해당 구간을 조회해 캐시에 병합한다. 기간이 없으면 기존 캐시의
    마지막 봉 이후만 증분 조회하고, 캐시가 없으면 가능한 전체 구간을 조회한다.
    """
    if start and end and start > end:
        raise ValueError("start date must be on or before end date")

    path = cache_path(data_root, cache_broker(region, exchange), timeframe.code, symbol)
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
        region=region,
        exchange=exchange,
    )
    frame = bars_to_frame(bars)
    if start is not None and not frame.empty:
        frame = frame.loc[frame.index >= pd.Timestamp(start)]
    if end is not None and not frame.empty:
        end_ts = pd.Timestamp(datetime.datetime.combine(end, datetime.time.max))
        frame = frame.loc[frame.index <= end_ts]
    if frame.empty:
        return existing if existing is not None else frame
    return merge_cache(path, frame)
