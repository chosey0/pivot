"""데이터 품질 진단 API (docs/04 §1.4·§3). 검사 로직은 pivot.diagnostics가
수행하고 여기서는 입력 로드·리포트 저장·직렬화만 한다. 진단은 읽기 전용이며
리포트는 diagnostic_reports에 입력 스냅샷과 함께 남는다."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, field_validator

from pivot.config import Timeframe
from pivot.dataset.build import run_preprocess
from pivot.dataset.samples import (
    SampleAccessError,
    overlap_stats_by_symbol,
    sample_split_stats,
)
from pivot.dataset.batch import SPLIT_METHOD
from pivot.labeling.fractal import confirmation_lag
from pivot.diagnostics import quality
from pivot.ingestion.cache import cache_path, load_cache
from pivot.ingestion.fetch import BROKER
from pivot.storage.datasets import DatasetNotFoundError
from pivot.storage.diagnostics import ReportNotFoundError
from pivot.storage.presets import PresetNotFoundError, validate_preset
from pivot.symbols.master import DOMESTIC_SYMBOL_RE
from server.deps import (
    DATA_ROOT,
    SHARD_CACHE_ROOT,
    dataset_repo,
    diagnostic_repo,
    object_storage,
    preset_repo,
)

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])


def _validated_symbols(symbols: list[str]) -> list[str]:
    cleaned = list(dict.fromkeys(symbol.strip() for symbol in symbols if symbol.strip()))
    invalid = [s for s in cleaned if not DOMESTIC_SYMBOL_RE.fullmatch(s)]
    if invalid:
        raise ValueError("symbols must be 6-digit domestic stock codes")
    if not cleaned:
        raise ValueError("symbols must not be empty")
    return cleaned


class CacheDiagnosticsRequest(BaseModel):
    symbols: list[str]
    timeframe: str = "day"

    @field_validator("symbols")
    @classmethod
    def _symbols(cls, symbols: list[str]) -> list[str]:
        return _validated_symbols(symbols)

    @field_validator("timeframe")
    @classmethod
    def _timeframe(cls, code: str) -> str:
        return Timeframe.from_code(code).code


class PreviewDiagnosticsRequest(BaseModel):
    preset_id: int
    symbols: list[str]

    @field_validator("symbols")
    @classmethod
    def _symbols(cls, symbols: list[str]) -> list[str]:
        return _validated_symbols(symbols)


@router.post("/cache")
def diagnose_cache(request: CacheDiagnosticsRequest) -> dict:
    frames = {
        symbol: load_cache(cache_path(DATA_ROOT, BROKER, request.timeframe, symbol))
        for symbol in request.symbols
    }
    report = quality.diagnose_cache(frames, timeframe=request.timeframe)
    return _save(report, target_type="raw_cache")


@router.post("/preview")
def diagnose_preview(request: PreviewDiagnosticsRequest) -> dict:
    try:
        preset_row = preset_repo().get(request.preset_id)
    except PresetNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    try:
        preset = validate_preset(
            preset_row["preset"], schema_version=preset_row["schema_version"]
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    timeframe = preset.timeframe.code
    results: dict[str, dict] = {}
    for symbol in request.symbols:
        df = load_cache(cache_path(DATA_ROOT, BROKER, timeframe, symbol))
        if df is None or df.empty:
            results[symbol] = {"error": f"no cached data for {symbol} ({timeframe})"}
            continue
        try:
            results[symbol] = run_preprocess(df, preset).stats
        except Exception as exc:  # 진단은 종목 실패도 리포트 항목으로 남긴다
            results[symbol] = {"error": str(exc)}

    report = quality.diagnose_preview(
        results,
        input_snapshot={
            "target": "preset",
            "preset_id": preset_row["id"],
            "preset_name": preset_row["name"],
            "preset_version": preset_row["version"],
            "preset": preset_row["preset"],
            "symbols": request.symbols,
        },
    )
    return _save(report, target_type="preset", preset_id=preset_row["id"])


@router.post("/datasets/{dataset_id}")
def diagnose_dataset(dataset_id: int) -> dict:
    repo = dataset_repo()
    try:
        dataset = repo.get(dataset_id)
    except DatasetNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    overlap = None
    overlap_error = None
    split_stats = None
    split_error = None
    if dataset["status"] == "ready":
        fractal = (
            ((dataset.get("preset_snapshot") or {}).get("preset") or {}).get("fractal")
            or {}
        )
        try:
            overlap = overlap_stats_by_symbol(
                repo,
                object_storage(),
                dataset_id,
                cache_root=SHARD_CACHE_ROOT,
                max_end_gap=confirmation_lag(int(fractal.get("n", 20))),
            )
        except (SampleAccessError, RuntimeError, ValueError) as exc:
            overlap_error = str(exc)
        split_config = (dataset.get("preset_snapshot") or {}).get("split") or {}
        if split_config.get("method") == SPLIT_METHOD:
            try:
                split_stats = sample_split_stats(
                    repo,
                    object_storage(),
                    dataset_id,
                    cache_root=SHARD_CACHE_ROOT,
                    seed=int(split_config["seed"]),
                )
            except (SampleAccessError, RuntimeError, ValueError) as exc:
                split_error = str(exc)
    report = quality.diagnose_dataset(
        dataset,
        repo.list_symbols(dataset_id),
        repo.list_shards(dataset_id),
        overlap_by_symbol=overlap,
        overlap_error=overlap_error,
        sample_split_stats=split_stats,
        sample_split_error=split_error,
    )
    return _save(
        report,
        target_type="dataset",
        preset_id=dataset["preset_id"],
        dataset_id=dataset_id,
    )


@router.get("")
def list_reports(
    target_type: Annotated[str | None, Query(pattern="^(raw_cache|preset|dataset)$")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict]:
    return diagnostic_repo().list(target_type=target_type, limit=limit)


@router.get("/{report_id}")
def get_report(report_id: int) -> dict:
    try:
        return diagnostic_repo().get(report_id)
    except ReportNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


def _save(
    report: dict,
    *,
    target_type: str,
    preset_id: int | None = None,
    dataset_id: int | None = None,
) -> dict:
    return diagnostic_repo().create(
        target_type=target_type,
        status=report["status"],
        summary={**report["summary"], "target": report["input"].get("target")},
        report={"checks": report["checks"], "input": report["input"]},
        preset_id=preset_id,
        dataset_id=dataset_id,
    )
