"""전처리 미리보기/일괄 처리 API. 핵심 로직은 pivot.dataset이 수행하고
여기서는 캐시 로드·직렬화·job 시작만 한다. preview와 batch는 같은
run_preprocess를 호출한다 (단일 파이프라인 원칙)."""

import functools
import re
from typing import Literal

import pandas as pd
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

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
from pivot.ingestion.fetch import BROKER, cache_broker
from pivot.storage.presets import PresetNotFoundError, validate_preset
from pivot.symbols.master import DOMESTIC_SYMBOL_RE
from server.deps import DATA_ROOT, dataset_repo, job_repo, object_storage, preset_repo
from server.jobs import start_background
from server.serialize import US_EASTERN, chart_payload, display_frame, time_value

router = APIRouter(prefix="/api/preprocess", tags=["preprocess"])


class PreviewRequest(BaseModel):
    symbol: str
    params: PreprocessPreset
    region: Literal["domestic", "overseas"] = "domestic"
    exchange: str = ""

    @model_validator(mode="after")
    def validate_instrument(self):
        self.symbol = _validated_symbol(self.symbol, self.region)
        self.exchange = self.exchange.strip().upper()
        cache_broker(self.region, self.exchange)
        return self


class InstrumentSource(BaseModel):
    region: Literal["domestic", "overseas"] = "domestic"
    exchange: str = ""

    @model_validator(mode="after")
    def validate_source(self):
        self.exchange = self.exchange.strip().upper()
        cache_broker(self.region, self.exchange)
        return self


class BatchRequest(BaseModel):
    preset_id: int
    dataset_name: str
    symbols: list[str]
    sources: dict[str, InstrumentSource] = Field(default_factory=dict)
    split_seed: int = DEFAULT_SPLIT_SEED

    @model_validator(mode="after")
    def validate_instruments(self):
        normalized_sources = {
            symbol.strip().upper(): source for symbol, source in self.sources.items()
        }
        self.symbols = [
            _validated_symbol(
                symbol,
                normalized_sources.get(symbol.strip().upper(), InstrumentSource()).region,
            )
            for symbol in self.symbols
        ]
        self.sources = normalized_sources
        return self


def _validated_symbol(symbol: str, region: str) -> str:
    normalized = symbol.strip().upper()
    valid = (
        DOMESTIC_SYMBOL_RE.fullmatch(normalized)
        if region == "domestic"
        else re.fullmatch(r"[A-Z0-9][A-Z0-9.-]*", normalized)
    )
    if not valid:
        raise ValueError(f"invalid {region} symbol: {symbol}")
    return normalized


@router.post("/preview")
def preview(request: PreviewRequest) -> dict:
    preset = request.params
    tf = preset.timeframe
    broker = cache_broker(request.region, request.exchange)
    df = load_cache(cache_path(DATA_ROOT, broker, tf.code, request.symbol))
    if df is None or df.empty:
        raise HTTPException(
            404, f"no cached data for {request.symbol} ({tf.code}) — run ingest first"
        )

    result = run_preprocess(df, preset)
    frame = result.frame
    source_timezone = US_EASTERN if request.region == "overseas" else None
    displayed = display_frame(frame, tf, source_timezone)
    times = [time_value(ts, tf) for ts in displayed.index]

    markers = [
        {
            "time": times[int(row.position)],
            "position": int(row.position),
            "kind": str(row.kind),
            "label": int(row.label),
            "price": float(row.price),
            "incoming_sample_label": (
                None
                if pd.isna(row.incoming_sample_label)
                else int(row.incoming_sample_label)
            ),
            "incoming_sample_included": bool(row.incoming_sample_included),
            "incoming_sample_index": (
                None
                if pd.isna(row.incoming_sample_index)
                else int(row.incoming_sample_index)
            ),
            "incoming_sample_drop_reason": row.incoming_sample_drop_reason,
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
        **chart_payload(displayed, tf, preset.ma_windows),
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
    sources = {
        symbol: request.sources.get(symbol, InstrumentSource()).model_dump()
        for symbol in symbols
    }
    try:
        dataset = datasets.create(
            name=name,
            preset_id=preset_row["id"],
            preset_snapshot=build_snapshot(
                preset_row,
                split_config(request.split_seed),
                preset=preset,
                sources=sources,
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
                "sources": sources,
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
            brokers={
                symbol: cache_broker(source["region"], source["exchange"])
                for symbol, source in sources.items()
            },
        )
    )
    return {"job_id": job["id"], "dataset_id": dataset["id"]}
