"""기간 파티션 parquet 캐시 입출력 (docs/04 §4)."""

import datetime
import os
import shutil
import tempfile
from pathlib import Path

import pandas as pd


def cache_path(data_root: Path, broker: str, timeframe_code: str, symbol: str) -> Path:
    """종목·타임프레임 캐시 디렉터리를 반환한다."""
    if broker == "kiwoom":
        market_path = Path("kiwoom") / "domestic"
    elif broker.startswith("kiwoom-overseas-"):
        exchange = broker.removeprefix("kiwoom-overseas-").upper()
        market_path = Path("kiwoom") / "overseas" / exchange
    else:
        market_path = Path(broker) / "domestic"
    return data_root / "raw" / market_path / symbol / timeframe_code


def _partition_files(path: Path) -> list[Path]:
    if not path.is_dir():
        return []
    return sorted(path.glob("*/part.parquet"))


def _with_time_index(df: pd.DataFrame, path: Path) -> pd.DataFrame:
    if len(df) == 0:
        return df.rename_axis("Time")
    if "Time" in df.columns:
        df = df.set_index("Time")
    if df.index.name != "Time":
        raise ValueError(f"cache has no Time index: {path}")
    return df.sort_index()


def _normalize(frame: pd.DataFrame, path: Path) -> pd.DataFrame:
    frame = _with_time_index(frame.copy(), path)
    return frame[~frame.index.duplicated(keep="last")].sort_index()


def _partition_name(timeframe_code: str, value: pd.Timestamp) -> str:
    if timeframe_code == "day":
        return f"year={value.year:04d}"
    return f"date={value.date().isoformat()}"


def _partitioned(frame: pd.DataFrame, path: Path) -> dict[str, pd.DataFrame]:
    groups: dict[str, list[int]] = {}
    for position, value in enumerate(frame.index):
        groups.setdefault(_partition_name(path.name, pd.Timestamp(value)), []).append(position)
    return {name: frame.iloc[positions] for name, positions in groups.items()}


def _atomic_write(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(dir=path.parent, suffix=".parquet", delete=False)
    temporary = Path(handle.name)
    handle.close()
    try:
        frame.to_parquet(temporary, row_group_size=10_000)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_cache(path: Path, columns: list[str] | None = None) -> pd.DataFrame | None:
    files = _partition_files(path)
    if not files:
        return None
    read_columns = None if columns is None else list(dict.fromkeys([*columns, "Time"]))
    frames = [
        _with_time_index(pd.read_parquet(file, columns=read_columns), file) for file in files
    ]
    return _normalize(pd.concat(frames), path)


def _empty_window(columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=columns).rename_axis("Time")


def load_cache_window(
    path: Path,
    *,
    before: pd.Timestamp | None = None,
    limit: int | None = None,
    lookback: int = 0,
    columns: list[str] | None = None,
) -> tuple[pd.DataFrame | None, bool]:
    """최신 파티션부터 차트 윈도우와 더 과거 데이터 존재 여부를 읽는다."""
    files = _partition_files(path)
    if not files:
        return None, False

    requested_columns = columns or ["Open", "High", "Low", "Close", "Volume"]
    read_columns = list(dict.fromkeys([*requested_columns, "Time"]))
    if limit is None:
        df = _normalize(
            pd.concat(
                [
                    _with_time_index(pd.read_parquet(file, columns=read_columns), file)
                    for file in files
                ]
            ),
            path,
        )
        if before is not None:
            df = df[df.index < before]
        return df, False

    rows_to_read = max(limit + lookback, limit)
    frames: list[pd.DataFrame] = []
    unread_older = False
    for index, file in enumerate(reversed(files)):
        frame = _with_time_index(pd.read_parquet(file, columns=read_columns), file)
        if before is not None:
            frame = frame[frame.index < before]
        if len(frame):
            frames.append(frame)
        if sum(len(item) for item in frames) >= rows_to_read:
            unread_older = index < len(files) - 1
            break

    if not frames:
        return _empty_window(requested_columns), False
    df = _normalize(pd.concat(reversed(frames)), path)
    return df.tail(rows_to_read), unread_older or len(df) > rows_to_read


def replace_cache(path: Path, frame: pd.DataFrame) -> pd.DataFrame:
    """캐시 전체를 파티션 단위로 교체한다."""
    normalized = _normalize(frame, path)
    if path.is_file():
        path.unlink()
    if len(normalized) == 0:
        delete_cache(path)
        return normalized

    partitions = _partitioned(normalized, path)
    for name, partition in partitions.items():
        _atomic_write(path / name / "part.parquet", partition)
    for existing in path.glob("*/part.parquet"):
        if existing.parent.name not in partitions:
            shutil.rmtree(existing.parent)
    return normalized


def merge_cache(path: Path, new_frame: pd.DataFrame) -> pd.DataFrame:
    """영향받은 기간 파티션만 병합한다. 같은 Time은 새 값으로 덮어쓴다."""
    normalized = _normalize(new_frame, path)
    for name, partition in _partitioned(normalized, path).items():
        destination = path / name / "part.parquet"
        if destination.exists():
            existing = _with_time_index(pd.read_parquet(destination), destination)
            partition = _normalize(pd.concat([existing, partition]), destination)
        _atomic_write(destination, partition)
    if len(normalized) == 0:
        return normalized
    merged = load_cache(path)
    return merged if merged is not None else normalized


def delete_cache(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def cache_status(
    path: Path,
    *,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
) -> dict | None:
    files = _partition_files(path)
    if not files:
        return None
    df = load_cache(path, columns=[])
    if df is None:
        return None
    if start is not None:
        df = df[df.index >= start]
    if end is not None:
        df = df[df.index <= end]
    if len(df) == 0:
        return None
    return {
        "bars": len(df),
        "first": df.index[0].isoformat(),
        "last": df.index[-1].isoformat(),
        "updated_at": datetime.datetime.fromtimestamp(
            max(file.stat().st_mtime for file in files)
        ).isoformat(),
    }
