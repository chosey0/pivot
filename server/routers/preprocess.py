"""전처리 미리보기/일괄 처리 API. 핵심 로직은 pivot.dataset이 수행하고
여기서는 캐시 로드·직렬화·job 시작만 한다. preview와 batch는 같은
run_preprocess를 호출한다 (단일 파이프라인 원칙)."""

import functools

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from pivot.config import PreprocessPreset
from pivot.dataset.batch import (
    DEFAULT_SPLIT_SEED,
    assign_splits,
    build_snapshot,
    run_batch,
    split_config,
)
from pivot.dataset.build import run_preprocess
from pivot.ingestion.cache import cache_path, load_cache
from pivot.ingestion.fetch import BROKER
from pivot.storage.presets import PresetNotFoundError, validate_preset
from pivot.symbols.master import DOMESTIC_SYMBOL_RE
from server.deps import DATA_ROOT, dataset_repo, job_repo, object_storage, preset_repo
from server.jobs import start_background
from server.serialize import chart_payload, time_value

router = APIRouter(prefix="/api/preprocess", tags=["preprocess"])


class PreviewRequest(BaseModel):
    symbol: str
    params: PreprocessPreset


class BatchRequest(BaseModel):
    preset_id: int
    dataset_name: str
    symbols: list[str]
    split_seed: int = DEFAULT_SPLIT_SEED

    @field_validator("symbols")
    @classmethod
    def validate_symbols(cls, symbols: list[str]) -> list[str]:
        invalid = [
            symbol
            for symbol in symbols
            if not DOMESTIC_SYMBOL_RE.fullmatch(symbol.strip())
        ]
        if invalid:
            raise ValueError("symbols must be 6-digit domestic stock codes")
        return symbols


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


@router.post("/batch")
def start_batch(request: BatchRequest) -> dict:
    """durable batch job을 만들고 백그라운드 스레드에서 실행한다."""
    name = request.dataset_name.strip()
    symbols = list(dict.fromkeys(symbol.strip() for symbol in request.symbols if symbol.strip()))
    if not name:
        raise HTTPException(422, "dataset_name is required")
    if not symbols:
        raise HTTPException(422, "symbols must not be empty")

    presets = preset_repo()
    try:
        preset_row = presets.get(request.preset_id)
    except PresetNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    if preset_row["archived_at"] is not None:
        raise HTTPException(409, f"preset {request.preset_id} is archived")
    try:
        preset = validate_preset(
            preset_row["preset"], schema_version=preset_row["schema_version"]
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    datasets = dataset_repo()
    if any(row["name"] == name for row in datasets.list()):
        raise HTTPException(409, f"dataset name {name!r} already exists")

    splits = assign_splits(symbols, seed=request.split_seed)
    try:
        dataset = datasets.create(
            name=name,
            preset_id=preset_row["id"],
            preset_snapshot=build_snapshot(
                preset_row,
                split_config(request.split_seed),
                preset=preset,
            ),
            timeframe=preset.timeframe.code,
            feature_columns=list(preset.features),
            symbols=symbols,
            splits=splits,
        )
    except Exception as exc:
        raise HTTPException(503, "failed to create dataset metadata") from exc

    jobs = job_repo()
    try:
        job = jobs.create(
            kind="preprocess_batch",
            payload={
                "dataset_id": dataset["id"],
                "dataset_name": name,
                "preset_id": preset_row["id"],
                "symbols": symbols,
            },
            total_items=len(symbols),
        )
    except Exception as exc:
        try:
            datasets.discard_building(dataset["id"])
        except Exception:
            try:
                datasets.mark_failed(dataset["id"], "durable job creation failed")
            except Exception:
                pass
        raise HTTPException(503, "failed to create durable batch job") from exc

    start_background(
        functools.partial(
            run_batch,
            jobs=jobs,
            datasets=datasets,
            storage=object_storage(),
            job_id=job["id"],
            dataset_id=dataset["id"],
            preset=preset,
            symbols=symbols,
            data_root=DATA_ROOT,
            broker=BROKER,
        )
    )
    return {"job_id": job["id"], "dataset_id": dataset["id"]}
