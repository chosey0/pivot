"""전처리 미리보기 API. 핵심 로직은 pivot.dataset.run_preprocess가 수행하고
여기서는 캐시 로드와 직렬화만 한다. M3 일괄 처리도 같은 pivot 함수를 호출한다."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from pivot.config import PreprocessPreset
from pivot.dataset.build import run_preprocess
from pivot.ingestion.cache import cache_path, load_cache
from pivot.ingestion.fetch import BROKER
from server.deps import DATA_ROOT
from server.serialize import chart_payload, time_value

router = APIRouter(prefix="/api/preprocess", tags=["preprocess"])


class PreviewRequest(BaseModel):
    symbol: str
    params: PreprocessPreset


@router.post("/preview")
def preview(request: PreviewRequest) -> dict:
    preset = request.params
    tf = preset.timeframe
    df = load_cache(cache_path(DATA_ROOT, BROKER, tf.code, request.symbol))
    if df is None or df.empty:
        raise HTTPException(
            404, f"no cached data for {request.symbol} ({tf.code}) — run ingest first"
        )

    result = run_preprocess(df, preset)
    frame = result.frame
    times = [time_value(ts, tf) for ts in frame.index]

    markers = [
        {
            "time": times[int(row.position)],
            "position": int(row.position),
            "kind": str(row.kind),
            "label": int(row.label),
            "price": float(row.price),
        }
        for row in result.points.itertuples()
    ]
    samples = [
        {
            "index": i,
            "label": sample.label,
            "kind": sample.kind,
            "length": sample.length,
            "start_time": times[sample.start_position],
            "end_time": times[sample.end_position],
            "start_position": sample.start_position,
            "end_position": sample.end_position,
        }
        for i, sample in enumerate(result.samples)
    ]

    return {
        "symbol": request.symbol,
        "timeframe": tf.code,
        **chart_payload(frame, tf, preset.ma_windows),
        "markers": markers,
        "samples": samples,
        "stats": result.stats,
        "features": {
            "columns": result.feature_columns,
            "dimension": len(result.feature_columns),
        },
    }
