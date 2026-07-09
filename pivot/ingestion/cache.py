"""parquet 캐시 입출력. 경로 규약: data/raw/{broker}/{timeframe}/{symbol}.parquet (docs/04 §4)."""

import datetime
from pathlib import Path

import pandas as pd


def cache_path(data_root: Path, broker: str, timeframe_code: str, symbol: str) -> Path:
    return data_root / "raw" / broker / timeframe_code / f"{symbol}.parquet"


def load_cache(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    return pd.read_parquet(path)


def merge_cache(path: Path, new_frame: pd.DataFrame) -> pd.DataFrame:
    """기존 캐시와 병합해 저장. 같은 Time은 새 값으로 덮어쓴다."""
    existing = load_cache(path)
    if existing is not None and not existing.empty:
        merged = pd.concat([existing, new_frame])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    else:
        merged = new_frame
    path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_parquet(path)
    return merged


def cache_status(path: Path) -> dict | None:
    df = load_cache(path)
    if df is None or df.empty:
        return None
    return {
        "bars": len(df),
        "first": df.index[0].isoformat(),
        "last": df.index[-1].isoformat(),
        "updated_at": datetime.datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
    }
