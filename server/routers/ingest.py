import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from pivot.config import Timeframe
from pivot.ingestion.cache import cache_path, cache_status
from pivot.ingestion.fetch import BROKER, update_cache
from server.deps import DATA_ROOT

router = APIRouter(prefix="/api/ingest", tags=["ingest"])


class IngestRequest(BaseModel):
    symbols: list[str]
    timeframe: str = "day"  # day | min{N} | tick{N}
    start: datetime.date | None = None
    end: datetime.date | None = None


# NOTE M1: 동기(await) 순차 수집. 종목이 많아지면 job + SSE 패턴으로 전환 (docs/04 §3)
@router.post("")
async def ingest(req: IngestRequest) -> dict:
    try:
        timeframe = Timeframe.from_code(req.timeframe)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if req.start and req.end and req.start > req.end:
        raise HTTPException(422, "start must be on or before end")

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
                    start=req.start,
                    end=req.end,
                )
                results[symbol] = {"ok": True, "bars": len(frame)}
            except Exception as e:  # 종목 단위 실패 격리
                results[symbol] = {"ok": False, "error": str(e)}
    return {"timeframe": timeframe.code, "results": results}


@router.get("/status")
def status(symbols: str, timeframe: str = "day") -> dict:
    tf = Timeframe.from_code(timeframe)
    return {
        symbol: cache_status(cache_path(DATA_ROOT, BROKER, tf.code, symbol))
        for symbol in symbols.split(",")
        if symbol
    }
