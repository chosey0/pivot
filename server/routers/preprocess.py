"""전처리 미리보기/일괄 처리 API. 핵심 로직은 pivot.dataset이 수행하고
여기서는 캐시 로드·직렬화·job 시작만 한다. preview와 batch는 같은
run_preprocess를 호출한다 (단일 파이프라인 원칙)."""

import functools
import datetime
import re
from typing import Literal

import pandas as pd
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from pivot.config import PreprocessPreset, Timeframe
from pivot.dataset.batch import (
    DEFAULT_SPLIT_SEED,
    build_snapshot,
    run_batch,
    split_config,
)
from pivot.dataset.build import run_preprocess
from pivot.ingestion.cache import cache_path, load_cache
from pivot.ingestion.fetch import (
    BROKER,
    DateBoundary,
    cache_broker,
    filter_overseas_day_market,
)
from pivot.storage.datasets import DatasetNotFoundError
from pivot.storage.presets import PresetNotFoundError, validate_preset
from pivot.symbols.master import DOMESTIC_SYMBOL_RE
from server.deps import DATA_ROOT, dataset_repo, job_repo, object_storage, preset_repo
from server.jobs import start_background
from server.serialize import (
    US_EASTERN,
    chart_payload,
    display_frame,
    market_time,
    time_value,
)

router = APIRouter(prefix="/api/preprocess", tags=["preprocess"])


class PreviewRequest(BaseModel):
    symbol: str
    params: PreprocessPreset
    region: Literal["domestic", "overseas"] = "domestic"
    exchange: str = ""
    start: DateBoundary | None = None
    end: DateBoundary | None = None

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


class BatchTarget(BaseModel):
    symbol: str
    timeframe: str
    region: Literal["domestic", "overseas"] = "domestic"
    exchange: str = ""
    start: DateBoundary | None = None
    end: DateBoundary | None = None

    @model_validator(mode="after")
    def validate_target(self):
        self.symbol = _validated_symbol(self.symbol, self.region)
        self.exchange = self.exchange.strip().upper()
        cache_broker(self.region, self.exchange)
        Timeframe.from_code(self.timeframe)
        _market_bounds(self.start, self.end, Timeframe.from_code(self.timeframe), self.region)
        return self


class BatchRequest(BaseModel):
    preset_id: int | None = None
    base_dataset_id: int | None = None
    dataset_name: str
    symbols: list[str]
    sources: dict[str, InstrumentSource] = Field(default_factory=dict)
    targets: list[BatchTarget] = Field(default_factory=list)
    split_seed: int = DEFAULT_SPLIT_SEED

    @model_validator(mode="after")
    def validate_instruments(self):
        if self.base_dataset_id is None and self.preset_id is None:
            raise ValueError("preset_id is required for a new dataset")
        if self.base_dataset_id is not None and (self.symbols or self.sources):
            raise ValueError("dataset extension accepts targets only")
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
        if len(self.symbols) != len(set(self.symbols)):
            raise ValueError("duplicate batch targets")
        self.sources = normalized_sources
        keys = [_target_identity(target) for target in self.targets]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate batch targets")
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


def _datetime_boundary(
    value: DateBoundary | None, *, end_of_day: bool
) -> datetime.datetime | None:
    if value is None or isinstance(value, datetime.datetime):
        return value
    return datetime.datetime.combine(
        value, datetime.time.max if end_of_day else datetime.time.min
    )


def _market_bounds(
    start: DateBoundary | None,
    end: DateBoundary | None,
    timeframe: Timeframe,
    region: str,
) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    source_timezone = US_EASTERN if region == "overseas" else None
    range_start = market_time(
        None
        if start is None
        else pd.Timestamp(_datetime_boundary(start, end_of_day=False)),
        timeframe,
        source_timezone,
    )
    range_end = market_time(
        None if end is None else pd.Timestamp(_datetime_boundary(end, end_of_day=True)),
        timeframe,
        source_timezone,
    )
    if range_start is not None and range_end is not None and range_start > range_end:
        raise ValueError("start must be on or before end")
    return range_start, range_end


def _target_identity(target: BatchTarget) -> tuple:
    return (
        target.region,
        target.exchange,
        target.symbol,
        target.timeframe,
        target.start,
        target.end,
    )


def _batch_target(target: BatchTarget) -> dict:
    timeframe = Timeframe.from_code(target.timeframe)
    cache_start, cache_end = _market_bounds(
        target.start, target.end, timeframe, target.region
    )
    return {
        **target.model_dump(mode="json"),
        "broker": cache_broker(target.region, target.exchange),
        "cache_start": None if cache_start is None else cache_start.isoformat(),
        "cache_end": None if cache_end is None else cache_end.isoformat(),
    }


@router.post("/preview")
def preview(request: PreviewRequest) -> dict:
    tf = request.params.timeframe
    preset = request.params.for_timeframe(tf)
    broker = cache_broker(request.region, request.exchange)
    df = load_cache(cache_path(DATA_ROOT, broker, tf.code, request.symbol))
    if df is None or df.empty:
        raise HTTPException(
            404, f"no cached data for {request.symbol} ({tf.code}) — run ingest first"
        )
    df = filter_overseas_day_market(df, tf, request.region)
    try:
        range_start, range_end = _market_bounds(
            request.start, request.end, tf, request.region
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if range_start is not None:
        df = df.loc[df.index >= range_start]
    if range_end is not None:
        df = df.loc[df.index <= range_end]
    if df.empty:
        raise HTTPException(404, "no candles in requested preprocess range")

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
    if not name:
        raise HTTPException(422, "dataset_name is required")

    datasets = dataset_repo()
    extended_from_dataset_id = request.base_dataset_id
    split_seed = request.split_seed
    if extended_from_dataset_id is not None:
        try:
            base_dataset = datasets.get(extended_from_dataset_id)
        except DatasetNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        if base_dataset["status"] != "ready":
            raise HTTPException(409, "only ready datasets can be extended")
        base_snapshot = base_dataset.get("preset_snapshot") or {}
        stored_targets = base_snapshot.get("targets") or []
        if not stored_targets:
            raise HTTPException(409, "base dataset has no reproducible targets")
        if not request.targets:
            raise HTTPException(422, "extension targets must not be empty")
        try:
            preset = validate_preset(
                base_snapshot["preset"],
                schema_version=int(base_snapshot["schema_version"]),
            )
            base_targets = [BatchTarget.model_validate(target) for target in stored_targets]
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(409, "base dataset snapshot is not reproducible") from exc
        preset_row = {
            "id": base_dataset["preset_id"],
            "name": base_snapshot.get("preset_name", preset.name),
            "version": int(base_snapshot.get("preset_version", 1)),
            "schema_version": int(base_snapshot["schema_version"]),
            "preset": base_snapshot["preset"],
        }
        split_seed = int((base_snapshot.get("split") or {}).get("seed", split_seed))
        requested_targets = [*base_targets, *request.targets]
    else:
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
        requested_targets = request.targets or [
            BatchTarget(
                symbol=symbol,
                timeframe=preset.timeframe.code,
                region=request.sources.get(symbol, InstrumentSource()).region,
                exchange=request.sources.get(symbol, InstrumentSource()).exchange,
            )
            for symbol in request.symbols
        ]
    if not requested_targets:
        raise HTTPException(422, "targets must not be empty")
    identities = [_target_identity(target) for target in requested_targets]
    if len(identities) != len(set(identities)):
        raise HTTPException(422, "extension target already exists in base dataset")
    targets = [_batch_target(target) for target in requested_targets]
    symbols = list(dict.fromkeys(target["symbol"] for target in targets))
    timeframes = list(dict.fromkeys(target["timeframe"] for target in targets))

    if any(row["name"] == name for row in datasets.list()):
        raise HTTPException(409, f"dataset name {name!r} already exists")

    sources = {
        target["symbol"]: {
            "region": target["region"],
            "exchange": target["exchange"],
        }
        for target in reversed(targets)
    }
    try:
        dataset = datasets.create(
            name=name,
            preset_id=preset_row["id"],
            preset_snapshot=build_snapshot(
                preset_row,
                split_config(split_seed),
                preset=preset,
                sources=sources,
                targets=[
                    {
                        key: target[key]
                        for key in ("symbol", "timeframe", "region", "exchange", "start", "end")
                    }
                    for target in targets
                ],
                extended_from_dataset_id=extended_from_dataset_id,
            ),
            timeframe=timeframes[0] if len(timeframes) == 1 else "mixed",
            feature_columns=list(preset.features),
            symbols=symbols,
            splits={},
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
                "targets": targets,
                "extended_from_dataset_id": extended_from_dataset_id,
            },
            total_items=len(targets),
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
            targets=targets,
            data_root=DATA_ROOT,
            broker=BROKER,
            split_seed=split_seed,
        )
    )
    return {"job_id": job["id"], "dataset_id": dataset["id"]}
