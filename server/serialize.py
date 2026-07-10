"""표준 DataFrame → lightweight-charts 데이터 직렬화 (docs/04 §5).

시간값 규약: 일봉 'yyyy-mm-dd' 문자열, 분/틱봉 unix 초. chart/preprocess
라우터가 같은 형식을 쓰도록 여기로 모은다.
"""

import math

import pandas as pd

from pivot.config import Timeframe

TimeValue = str | int


def time_value(ts: pd.Timestamp, timeframe: Timeframe) -> TimeValue:
    if timeframe.type == "day":
        return ts.date().isoformat()
    return int(ts.timestamp())


def chart_payload(
    df: pd.DataFrame, timeframe: Timeframe, ma_windows: tuple[int, ...] | list[int]
) -> dict:
    """candles/volumes/ma 페이로드. df에는 요청한 MA 컬럼이 있어야 한다."""
    times = [time_value(ts, timeframe) for ts in df.index]
    candles = [
        {"time": t, "open": o, "high": h, "low": low, "close": c}
        for t, o, h, low, c in zip(
            times, df["Open"], df["High"], df["Low"], df["Close"]
        )
    ]
    volumes = [{"time": t, "value": int(v)} for t, v in zip(times, df["Volume"])]
    ma = {
        str(w): [
            {"time": t, "value": v}
            for t, v in zip(times, df[str(w)])
            if not math.isnan(v)
        ]
        for w in ma_windows
    }
    return {"candles": candles, "volumes": volumes, "ma": ma}
