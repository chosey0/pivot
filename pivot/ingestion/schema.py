"""ChartBar → 표준 DataFrame 변환. 데이터 계약: docs/03 §5.

표준 스키마: DatetimeIndex(이름 Time, 과거→최근 오름차순) +
Open/High/Low/Close(float64), Volume(int64), Amount(float64, 결측 가능).
이평선 컬럼("5", "20", "120", …)은 indicators.add_moving_averages가 추가한다.
"""

from typing import TYPE_CHECKING, Sequence

import pandas as pd

if TYPE_CHECKING:
    from brokers.kiwoom.models.ohlcv import ChartBar

COLUMNS = ["Open", "High", "Low", "Close", "Volume", "Amount"]

# Kiwoom timestamp 원본 포맷: 일봉 YYYYMMDD, 분/틱봉 YYYYMMDDHHMMSS
_TIMESTAMP_FORMATS = {8: "%Y%m%d", 14: "%Y%m%d%H%M%S"}


def _parse_timestamp(value: str) -> pd.Timestamp:
    text = value.strip()
    fmt = _TIMESTAMP_FORMATS.get(len(text))
    if fmt is not None:
        return pd.to_datetime(text, format=fmt)
    return pd.to_datetime(text)  # SDK가 정규화한 포맷 대비 폴백


def bars_to_frame(bars: Sequence["ChartBar"]) -> pd.DataFrame:
    if not bars:
        return pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], name="Time"))

    frame = pd.DataFrame(
        {
            "Time": [_parse_timestamp(bar.timestamp) for bar in bars],
            "Open": [float(bar.open) for bar in bars],
            "High": [float(bar.high) for bar in bars],
            "Low": [float(bar.low) for bar in bars],
            "Close": [float(bar.close) for bar in bars],
            "Volume": [int(bar.volume) for bar in bars],
            "Amount": [float(bar.amount) if bar.amount is not None else None for bar in bars],
        }
    )
    frame = frame.sort_values("Time").drop_duplicates("Time", keep="last")
    frame = frame.set_index("Time")
    frame["Amount"] = frame["Amount"].astype("float64")
    return frame
