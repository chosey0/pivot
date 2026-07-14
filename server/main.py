import datetime
import random
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from server.routers import (
    chart,
    datasets,
    diagnostics,
    ingest,
    jobs,
    live,
    preprocess,
    presets,
    runs,
    symbols,
    watchlist,
)
from server.deps import DATA_ROOT
from server.jobs import stop_processes
from server.live import LiveService


@asynccontextmanager
async def lifespan(app: FastAPI):
    service = LiveService(DATA_ROOT)
    app.state.live = service
    await service.start()
    try:
        yield
    finally:
        try:
            await service.close()
        finally:
            await asyncio.to_thread(stop_processes)


app = FastAPI(title="pivot workbench", lifespan=lifespan)
app.include_router(watchlist.router)
app.include_router(ingest.router)
app.include_router(chart.router)
app.include_router(preprocess.router)
app.include_router(symbols.router)
app.include_router(presets.router)
app.include_router(jobs.router)
app.include_router(datasets.router)
app.include_router(diagnostics.router)
app.include_router(runs.router)
app.include_router(live.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}


# M0 확인용 더미 캔들. M1에서 pivot.ingestion 캐시 조회로 교체된다.
@app.get("/api/chart/dummy")
def chart_dummy(bars: int = 120, seed: int = 42):
    rng = random.Random(seed)
    start = datetime.date.today() - datetime.timedelta(days=bars * 2)

    candles = []
    day = start
    close = 50_000.0
    while len(candles) < bars:
        if day.weekday() < 5:  # 주말 제외
            open_ = close
            close = open_ * (1 + rng.gauss(0, 0.02))
            high = max(open_, close) * (1 + abs(rng.gauss(0, 0.008)))
            low = min(open_, close) * (1 - abs(rng.gauss(0, 0.008)))
            candles.append({
                "time": day.isoformat(),
                "open": round(open_),
                "high": round(high),
                "low": round(low),
                "close": round(close),
            })
        day += datetime.timedelta(days=1)

    return {"symbol": "DUMMY", "timeframe": "day", "candles": candles}
