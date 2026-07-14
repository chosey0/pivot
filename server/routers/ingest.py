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


class IngestRequest(BaseModel):
    symbols: list[str]
    timeframe: str = "day"  # day | min{N} | tick{N}
    start: DateBoundary | None = None
    end: DateBoundary | None = None
    region: Region = "domestic"
    exchange: str = ""


def _warmup_start(
    start: datetime.datetime | None, timeframe: Timeframe
) -> datetime.datetime | None:
    if start is None or timeframe.type != "minute":
        return start
    return start - datetime.timedelta(
        minutes=MA_WARMUP_PERIODS * timeframe.unit
    )


# NOTE M1: 동기(await) 순차 수집. 종목이 많아지면 job + SSE 패턴으로 전환 (docs/04 §3)
@router.post("")
async def ingest(req: IngestRequest) -> dict:
    try:
        timeframe = Timeframe.from_code(req.timeframe)
    except ValueError as e:
        raise HTTPException(422, str(e))
    start = (
        req.start
        if isinstance(req.start, datetime.datetime)
        else datetime.datetime.combine(req.start, datetime.time.min)
        if req.start
        else None
    )
    end = (
        req.end
        if isinstance(req.end, datetime.datetime)
        else datetime.datetime.combine(req.end, datetime.time.max)
        if req.end
        else None
    )
    if start and end and start > end:
        raise HTTPException(422, "start must be on or before end")
    try:
        cache_broker(req.region, req.exchange)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    market_timezone = US_EASTERN if req.region == "overseas" else None
    fetch_start = _warmup_start(start, timeframe)
    market_start = market_time(
        None if fetch_start is None else pd.Timestamp(fetch_start),
        timeframe,
        market_timezone,
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
) -> dict:
    tf = Timeframe.from_code(timeframe)
    broker = cache_broker(region, exchange)
    source_timezone = US_EASTERN if region == "overseas" else None
    result = {}
    for symbol in (value for value in symbols.split(",") if value):
        row = cache_status(cache_path(DATA_ROOT, broker, tf.code, symbol))
        if row is not None and source_timezone is not None:
            for field in ("first", "last"):
                row[field] = display_timestamp(
                    pd.Timestamp(row[field]), tf, source_timezone
                ).isoformat()
        result[symbol] = row
    return result
