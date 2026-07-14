import datetime

import pandas as pd
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from pivot.config import Timeframe
from pivot.ingestion.cache import cache_path, cache_status
from pivot.ingestion.fetch import DateBoundary, Region, cache_broker, update_cache
from pivot.ingestion.indicators import DEFAULT_WINDOWS
from server.deps import DATA_ROOT
from server.serialize import US_EASTERN, display_timestamp, market_time

router = APIRouter(prefix="/api/ingest", tags=["ingest"])
MA_WARMUP_PERIODS = max(DEFAULT_WINDOWS)


def _datetime_boundary(
    value: DateBoundary | None, *, end_of_day: bool
) -> datetime.datetime | None:
    if value is None or isinstance(value, datetime.datetime):
        return value
    return datetime.datetime.combine(
        value, datetime.time.max if end_of_day else datetime.time.min
    )


class IngestRequest(BaseModel):
    symbols: list[str]
    timeframe: str = "day"  # day | min{N} | tick{N}
    start: DateBoundary | None = None
    end: DateBoundary | None = None
    region: Region = "domestic"
    exchange: str = ""


# NOTE M1: 동기(await) 순차 수집. 종목이 많아지면 job + SSE 패턴으로 전환 (docs/04 §3)
@router.post("")
async def ingest(req: IngestRequest) -> dict:
    try:
        timeframe = Timeframe.from_code(req.timeframe)
    except ValueError as e:
        raise HTTPException(422, str(e))
    start = _datetime_boundary(req.start, end_of_day=False)
    end = _datetime_boundary(req.end, end_of_day=True)
    if start and end and start > end:
        raise HTTPException(422, "start must be on or before end")
    try:
        cache_broker(req.region, req.exchange)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    market_timezone = US_EASTERN if req.region == "overseas" else None
    market_start = market_time(
        None if start is None else pd.Timestamp(start), timeframe, market_timezone
    )
    market_end = market_time(
        None if end is None else pd.Timestamp(end), timeframe, market_timezone
    )

    try:
        from brokers.kiwoom import Credentials, KiwoomClient
    except ImportError as e:
        raise HTTPException(500, f"broker-modules not installed: {e}")

    results: dict[str, dict] = {}
    async with KiwoomClient(credentials=Credentials.from_env()) as client:
        for symbol in req.symbols:
            try:
                frame = await update_cache(
                    client,
                    symbol,
                    timeframe,
                    DATA_ROOT,
                    start=market_start,
                    end=market_end,
                    warmup_bars=(
                        MA_WARMUP_PERIODS if timeframe.type == "minute" else 0
                    ),
                    region=req.region,
                    exchange=req.exchange,
                )
                results[symbol] = {"ok": True, "bars": len(frame)}
            except Exception as e:  # 종목 단위 실패 격리
                results[symbol] = {"ok": False, "error": str(e)}
    return {"timeframe": timeframe.code, "results": results}


@router.get("/status")
def status(
    symbols: str,
    timeframe: str = "day",
    region: Region = "domestic",
    exchange: str = "",
    start: DateBoundary | None = None,
    end: DateBoundary | None = None,
) -> dict:
    tf = Timeframe.from_code(timeframe)
    broker = cache_broker(region, exchange)
    source_timezone = US_EASTERN if region == "overseas" else None
    range_start = market_time(
        None
        if start is None
        else pd.Timestamp(_datetime_boundary(start, end_of_day=False)),
        tf,
        source_timezone,
    )
    range_end = market_time(
        None if end is None else pd.Timestamp(_datetime_boundary(end, end_of_day=True)),
        tf,
        source_timezone,
    )
    if range_start is not None and range_end is not None and range_start > range_end:
        raise HTTPException(422, "start must be on or before end")
    result = {}
    for symbol in (value for value in symbols.split(",") if value):
        row = cache_status(
            cache_path(DATA_ROOT, broker, tf.code, symbol),
            start=range_start,
            end=range_end,
        )
        if row is not None and source_timezone is not None:
            for field in ("first", "last"):
                row[field] = display_timestamp(
                    pd.Timestamp(row[field]), tf, source_timezone
                ).isoformat()
        result[symbol] = row
    return result
