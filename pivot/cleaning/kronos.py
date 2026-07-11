"""Kronos Appendix B 기반 K-line 클리닝 경계 분석.

원천 DataFrame은 절대 수정하지 않는다. 가격 결측/불변식 위반, 구조적 가격
점프, 장기 비유동, 가격 정체를 경계로 정상 세그먼트를 반환한다. 논문의
frequency별 값은 분봉/일봉 기본값으로 사용하되, 최소 길이는 Pivot의 MA와
프랙탈 요구 길이를 사용한다. 틱봉에는 논문 대응 빈도가 없어 자동 휴리스틱을
적용하지 않고 필드 무결성만 검사한다.

Reference: Shi et al., "Kronos: A Foundation Model for the Language of
Financial Markets", arXiv:2508.02739, Appendix B.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from pivot.config import CleaningConfig, Timeframe

PAPER_URL = "https://arxiv.org/abs/2508.02739"
POLICY_VERSION = "kronos_adapted_v1"

# price jump, max consecutive illiquid bars, max consecutive stagnant bars.
# 프로젝트에 없는 3/45분봉은 더 보수적인 인접 상위 빈도 값을 사용한다.
_FREQUENCY_DEFAULTS: dict[str, tuple[float, int, int]] = {
    "day": (0.30, 1, 3),
    "min1": (0.10, 15, 45),
    "min3": (0.15, 3, 10),
    "min5": (0.15, 3, 10),
    "min10": (0.15, 3, 6),
    "min15": (0.15, 2, 5),
    "min30": (0.20, 2, 3),
    "min45": (0.20, 1, 3),
    "min60": (0.20, 1, 3),
}

_SOURCE_FREQUENCIES = {
    "day": "day",
    "min1": "min1",
    "min3": "min5",
    "min5": "min5",
    "min10": "min10",
    "min15": "min15",
    "min30": "min30",
    "min45": "min60",
    "min60": "min60",
}


@dataclass(frozen=True)
class CleanSegment:
    start: int
    end: int  # inclusive

    @property
    def length(self) -> int:
        return self.end - self.start + 1


@dataclass(frozen=True)
class CleaningAnalysis:
    segments: tuple[CleanSegment, ...]
    invalid_positions: tuple[int, ...]
    structural_breaks: tuple[int, ...]  # 새 세그먼트가 시작되는 위치
    reasons: dict[str, tuple[int, ...]]
    thresholds: dict[str, Any]
    original_bars: int
    retained_bars: int

    def to_stats(self) -> dict:
        removed = self.original_bars - self.retained_bars
        return {
            "policy": POLICY_VERSION,
            "reference": PAPER_URL,
            "original_bars": self.original_bars,
            "retained_bars": self.retained_bars,
            "removed_bars": removed,
            "removed_ratio": round(removed / self.original_bars, 6)
            if self.original_bars
            else 0.0,
            "segments": len(self.segments),
            "segment_lengths": [segment.length for segment in self.segments],
            "structural_breaks": len(self.structural_breaks),
            "reason_counts": {name: len(values) for name, values in self.reasons.items()},
            "thresholds": self.thresholds,
        }


def analyze_kline_quality(
    df: pd.DataFrame,
    *,
    timeframe: Timeframe,
    config: CleaningConfig,
    required_bars: int,
) -> CleaningAnalysis:
    """저품질 위치와 정상 세그먼트를 계산한다. 입력은 변경하지 않는다."""
    size = len(df)
    thresholds = _resolve_thresholds(timeframe, config, required_bars)
    if size == 0:
        return CleaningAnalysis((), (), (), {}, thresholds, 0, 0)

    hard_invalid = _hard_invalid_positions(df)
    price_jump = _price_jump_boundaries(df, thresholds["price_jump_threshold"])
    illiquid = _invalid_runs(
        _illiquid_mask(df), thresholds["max_illiquid_bars"]
    )
    stagnant = _invalid_runs(
        _stagnant_mask(df), thresholds["max_stagnant_bars"]
    )

    invalid = hard_invalid | illiquid | stagnant
    candidates = _split_segments(size, invalid, price_jump)
    min_length = thresholds["min_segment_bars"]
    segments = tuple(segment for segment in candidates if segment.length >= min_length)
    retained = sum(segment.length for segment in segments)
    too_short = {
        position
        for segment in candidates
        if segment.length < min_length
        for position in range(segment.start, segment.end + 1)
    }
    reasons = {
        "invalid_price": tuple(sorted(hard_invalid)),
        "illiquid": tuple(sorted(illiquid)),
        "stagnant": tuple(sorted(stagnant)),
        "too_short": tuple(sorted(too_short)),
    }
    return CleaningAnalysis(
        segments=segments,
        invalid_positions=tuple(sorted(invalid | too_short)),
        structural_breaks=tuple(sorted(price_jump)),
        reasons=reasons,
        thresholds=thresholds,
        original_bars=size,
        retained_bars=retained,
    )


def _resolve_thresholds(
    timeframe: Timeframe, config: CleaningConfig, required_bars: int
) -> dict[str, Any]:
    defaults = _FREQUENCY_DEFAULTS.get(timeframe.code)
    return {
        "timeframe": timeframe.code,
        "source_frequency": _SOURCE_FREQUENCIES.get(timeframe.code),
        "price_jump_threshold": config.price_jump_threshold
        if config.price_jump_threshold is not None
        else (defaults[0] if defaults else None),
        "max_illiquid_bars": config.max_illiquid_bars
        if config.max_illiquid_bars is not None
        else (defaults[1] if defaults else None),
        "max_stagnant_bars": config.max_stagnant_bars
        if config.max_stagnant_bars is not None
        else (defaults[2] if defaults else None),
        "min_segment_bars": config.min_segment_bars or max(required_bars, 1),
        "stagnation_rule": "same_close_and_inactive",
    }


def _hard_invalid_positions(df: pd.DataFrame) -> set[int]:
    required = ["Open", "High", "Low", "Close"]
    values = df[required].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)
    finite = np.isfinite(values).all(axis=1)
    positive = (values > 0).all(axis=1)
    open_, high, low, close = values.T
    invariant = (low <= open_) & (open_ <= high) & (low <= close) & (close <= high)
    return set(np.flatnonzero(~(finite & positive & invariant)).tolist())


def _price_jump_boundaries(df: pd.DataFrame, threshold: float | None) -> set[int]:
    if threshold is None or len(df) < 2:
        return set()
    previous_close = pd.to_numeric(df["Close"], errors="coerce").shift(1)
    current_open = pd.to_numeric(df["Open"], errors="coerce")
    ratio = (current_open - previous_close).abs() / previous_close.abs()
    return set(np.flatnonzero((ratio > threshold).fillna(False).to_numpy()).tolist())


def _illiquid_mask(df: pd.DataFrame) -> np.ndarray:
    if "Volume" not in df.columns:
        return np.zeros(len(df), dtype=bool)
    volume = pd.to_numeric(df["Volume"], errors="coerce")
    return volume.fillna(0).le(0).to_numpy()


def _stagnant_mask(df: pd.DataFrame) -> np.ndarray:
    close = pd.to_numeric(df["Close"], errors="coerce")
    same_close = close.eq(close.shift(1)).fillna(False)
    flat_bar = pd.to_numeric(df["High"], errors="coerce").eq(
        pd.to_numeric(df["Low"], errors="coerce")
    )
    inactive = pd.Series(_illiquid_mask(df), index=df.index) | flat_bar
    return (same_close & inactive).to_numpy()


def _invalid_runs(mask: np.ndarray, max_consecutive: int | None) -> set[int]:
    if max_consecutive is None:
        return set()
    invalid: set[int] = set()
    start: int | None = None
    for position, flagged in enumerate([*mask, False]):
        if flagged and start is None:
            start = position
        elif not flagged and start is not None:
            if position - start > max_consecutive:
                invalid.update(range(start, position))
            start = None
    return invalid


def _split_segments(
    size: int, invalid: set[int], structural_breaks: set[int]
) -> list[CleanSegment]:
    segments: list[CleanSegment] = []
    start: int | None = None
    for position in range(size):
        if position in invalid:
            if start is not None:
                segments.append(CleanSegment(start, position - 1))
                start = None
            continue
        if position in structural_breaks and start is not None:
            segments.append(CleanSegment(start, position - 1))
            start = position
        elif start is None:
            start = position
    if start is not None:
        segments.append(CleanSegment(start, size - 1))
    return segments
