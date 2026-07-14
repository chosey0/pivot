"""표준 DataFrame → lightweight-charts 데이터 직렬화 (docs/04 §5).

시간값 규약: 일봉 'yyyy-mm-dd' 문자열, 분/틱봉 unix 초. chart/preprocess
라우터가 같은 형식을 쓰도록 여기로 모은다.
"""

import math
from zoneinfo import ZoneInfo

import pandas as pd

from pivot.config import Timeframe

TimeValue = str | int
KST = ZoneInfo("Asia/Seoul")
US_EASTERN = ZoneInfo("America/New_York")
DAILY_MARKET_CLOSE_HOUR = 16


def time_value(ts: pd.Timestamp, timeframe: Timeframe) -> TimeValue:
    if timeframe.type == "day":
        return ts.date().isoformat()
    return int(ts.timestamp())


def display_frame(
    frame: pd.DataFrame,
    timeframe: Timeframe,
    source_timezone: ZoneInfo | None = None,
) -> pd.DataFrame:
    if frame.empty or source_timezone is None:
        return frame
    displayed = frame.copy()
    displayed.index = pd.DatetimeIndex(
        [
            display_timestamp(value, timeframe, source_timezone)
            for value in frame.index
        ],
        name=frame.index.name,
    )
    return displayed


def display_time_value(
    value: pd.Timestamp,
    timeframe: Timeframe,
    source_timezone: ZoneInfo | None = None,
) -> TimeValue:
    if source_timezone is not None:
        value = display_timestamp(value, timeframe, source_timezone)
    return time_value(value, timeframe)


def display_timestamp(
    value: pd.Timestamp,
    timeframe: Timeframe,
    source_timezone: ZoneInfo | None = None,
) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if source_timezone is None:
        return timestamp
    if timeframe.type == "day":
        # 일봉은 거래소 정규장 종료 시각이 속하는 KST 날짜로 표시한다.
        timestamp = timestamp.normalize() + pd.Timedelta(
            hours=DAILY_MARKET_CLOSE_HOUR
        )
        return _convert_timezone(timestamp, source_timezone, KST).normalize()
    return _convert_timezone(timestamp, source_timezone, KST)


def market_time(
    value: pd.Timestamp | None,
    timeframe: Timeframe,
    market_timezone: ZoneInfo | None = None,
) -> pd.Timestamp | None:
    if value is None or market_timezone is None:
        return value
    if timeframe.type == "day":
        # display_timestamp의 역변환: KST 날짜가 시작되는 순간의 미국 거래일.
        timestamp = pd.Timestamp(value).normalize()
        localized = timestamp.tz_localize(KST)
        return pd.Timestamp(localized.tz_convert(market_timezone).date())
    return _convert_timezone(value, KST, market_timezone)


def _convert_timezone(
    value: pd.Timestamp, source: ZoneInfo, destination: ZoneInfo
) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    localized = (
        timestamp.tz_localize(source)
        if timestamp.tzinfo is None
        else timestamp.tz_convert(source)
    )
    return localized.tz_convert(destination).tz_localize(None)


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
