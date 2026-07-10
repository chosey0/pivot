import datetime
import random

from fastapi import FastAPI

from server.routers import chart, ingest, preprocess, watchlist

app = FastAPI(title="pivot workbench")
app.include_router(watchlist.router)
app.include_router(ingest.router)
app.include_router(chart.router)
app.include_router(preprocess.router)


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
