"""이동평균 계산. HTS 제공값 대신 직접 계산한다 (docs/03 §5).

ma_source="daily"(분/틱봉에 일봉 이평 병합, 구 프로젝트 방식)는 미구현 — 백로그 참고.
"""

from typing import Iterable

import pandas as pd

DEFAULT_WINDOWS = (5, 20, 60, 120)


def add_moving_averages(
    df: pd.DataFrame,
    windows: Iterable[int] = DEFAULT_WINDOWS,
) -> pd.DataFrame:
    """Close 단순이동평균 컬럼을 추가한 사본 반환. 컬럼명은 str(window)."""
    out = df.copy()
    for window in windows:
        out[str(window)] = out["Close"].rolling(window).mean()
    return out
