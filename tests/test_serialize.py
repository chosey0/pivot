from zoneinfo import ZoneInfo

import pandas as pd

from pivot.config import Timeframe
from server.serialize import display_frame, market_time, time_value


def test_overseas_intraday_display_round_trips_through_kst():
    timeframe = Timeframe.from_code("min1")
    eastern = ZoneInfo("America/New_York")
    source = pd.DataFrame(
        {"Close": [225.5]},
        index=pd.DatetimeIndex(["2026-07-14 09:30:00"], name="Time"),
    )

    displayed = display_frame(source, timeframe, eastern)
    encoded = time_value(displayed.index[0], timeframe)
    decoded = pd.to_datetime(encoded, unit="s")

    assert displayed.index[0] == pd.Timestamp("2026-07-14 22:30:00")
    assert market_time(decoded, timeframe, eastern) == source.index[0]


def test_overseas_daily_session_uses_kst_close_date_and_round_trips():
    timeframe = Timeframe.from_code("day")
    source = pd.DataFrame(
        {"Close": [225.5]},
        index=pd.DatetimeIndex(["2026-07-14"], name="Time"),
    )

    displayed = display_frame(source, timeframe, ZoneInfo("America/New_York"))

    assert displayed.index[0] == pd.Timestamp("2026-07-15")
    assert market_time(
        displayed.index[0], timeframe, ZoneInfo("America/New_York")
    ) == pd.Timestamp("2026-07-14")
