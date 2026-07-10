"""parquet 캐시 입출력. 경로 규약: data/raw/{broker}/{timeframe}/{symbol}.parquet (docs/04 §4)."""

import datetime
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq


def cache_path(data_root: Path, broker: str, timeframe_code: str, symbol: str) -> Path:
    return data_root / "raw" / broker / timeframe_code / f"{symbol}.parquet"


def load_cache(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    return pd.read_parquet(path)


def _time_bounds(row_group: pq.RowGroupMetaData) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    for index in range(row_group.num_columns):
        column = row_group.column(index)
        if column.path_in_schema != "Time" or column.statistics is None:
            continue
        return pd.Timestamp(column.statistics.min), pd.Timestamp(column.statistics.max)
    return None


def _with_time_index(df: pd.DataFrame, path: Path) -> pd.DataFrame:
    if df.empty:
        return df
    if "Time" in df.columns:
        df = df.set_index("Time")
    if df.index.name != "Time":
        raise ValueError(f"cache has no Time index: {path}")
    return df.sort_index()


def _empty_window(columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=columns).rename_axis("Time")


def _read_row_groups(
    parquet_file: pq.ParquetFile,
    group_indexes: list[int],
    columns: list[str],
    path: Path,
) -> pd.DataFrame:
    if not group_indexes:
        return _empty_window(columns)
    read_columns = list(dict.fromkeys([*columns, "Time"]))
    table = parquet_file.read_row_groups(sorted(group_indexes), columns=read_columns)
    return _with_time_index(table.to_pandas(), path)


def load_cache_window(
    path: Path,
    *,
    before: pd.Timestamp | None = None,
    limit: int | None = None,
    lookback: int = 0,
    columns: list[str] | None = None,
) -> tuple[pd.DataFrame | None, bool]:
    """차트용 캐시 윈도우를 읽는다.

    반환 DataFrame은 MA 계산용 lookback을 포함할 수 있고, bool은 요청 윈도우보다
    더 과거 데이터가 있는지 나타낸다.
    """
    if not path.exists():
        return None, False

    requested_columns = columns or ["Open", "High", "Low", "Close", "Volume"]

    if limit is None:
        df = _with_time_index(pd.read_parquet(path, columns=requested_columns), path)
        if before is not None:
            df = df[df.index < before]
        return df, False

    rows_to_read = max(limit + lookback, limit)
    parquet_file = pq.ParquetFile(path)
    candidate_groups: list[tuple[int, int]] = []
    for index in range(parquet_file.num_row_groups):
        row_group = parquet_file.metadata.row_group(index)
        bounds = _time_bounds(row_group)
        if before is not None and bounds is not None and bounds[0] >= before:
            continue
        candidate_groups.append((index, row_group.num_rows))

    selected_groups: list[int] = []
    remaining_groups = list(reversed(candidate_groups))
    estimated_rows = 0
    while remaining_groups and estimated_rows < rows_to_read:
        index, row_count = remaining_groups.pop(0)
        selected_groups.append(index)
        estimated_rows += row_count

    df = _read_row_groups(parquet_file, selected_groups, requested_columns, path)
    if before is not None:
        df = df[df.index < before]

    while len(df) < rows_to_read and remaining_groups:
        index, _ = remaining_groups.pop(0)
        selected_groups.append(index)
        df = _read_row_groups(parquet_file, selected_groups, requested_columns, path)
        if before is not None:
            df = df[df.index < before]

    has_more = len(df) > rows_to_read
    has_more = has_more or bool(remaining_groups)
    return df.tail(rows_to_read), has_more


def merge_cache(path: Path, new_frame: pd.DataFrame) -> pd.DataFrame:
    """기존 캐시와 병합해 저장. 같은 Time은 새 값으로 덮어쓴다."""
    existing = load_cache(path)
    if existing is not None and not existing.empty:
        merged = pd.concat([existing, new_frame])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    else:
        merged = new_frame
    path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_parquet(path, row_group_size=10_000)
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
