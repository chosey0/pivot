"""Kronos 적응형 클리닝의 경계와 전처리 통합 계약."""

import numpy as np
import pandas as pd

from pivot.cleaning.kronos import analyze_kline_quality
from pivot.config import CleaningConfig, FractalConfig, LabelingConfig, PreprocessPreset, Timeframe
from pivot.dataset.build import run_preprocess


def candles(length: int = 80) -> pd.DataFrame:
    close = 100 + np.sin(np.arange(length) / 3) * 5
    index = pd.date_range("2026-01-01", periods=length, freq="D", name="Time")
    return pd.DataFrame(
        {
            "Open": close,
            "High": close + 1,
            "Low": close - 1,
            "Close": close,
            "Volume": 1000,
            "Amount": 1_000_000,
        },
        index=index,
    )


def preset(mode: str) -> PreprocessPreset:
    return PreprocessPreset(
        name="cleaning-test",
        timeframe=Timeframe(type="day"),
        fractal=FractalConfig(n=5),
        labeling=LabelingConfig(ignore_rule="none"),
        ma_windows=[5],
        features=["Open", "High", "Low", "Close"],
        cleaning=CleaningConfig(mode=mode, min_segment_bars=5),
    )


def test_price_missing_and_ohlc_violation_are_hard_boundaries():
    frame = candles(30)
    frame.iloc[7, frame.columns.get_loc("Close")] = np.nan
    frame.iloc[18, frame.columns.get_loc("High")] = frame.iloc[18]["Low"] - 1

    analysis = analyze_kline_quality(
        frame,
        timeframe=Timeframe(type="day"),
        config=CleaningConfig(mode="filter", min_segment_bars=3),
        required_bars=3,
    )

    assert analysis.reasons["invalid_price"] == (7, 18)
    assert [(item.start, item.end) for item in analysis.segments] == [
        (0, 6),
        (8, 17),
        (19, 29),
    ]


def test_jump_illiquidity_and_stagnation_split_segments():
    frame = candles(40)
    frame.iloc[12, frame.columns.get_loc("Open")] = frame.iloc[11]["Close"] * 1.5
    frame.iloc[20:23, frame.columns.get_loc("Volume")] = 0
    frame.iloc[30:35, frame.columns.get_loc("Close")] = 90
    frame.iloc[30:35, frame.columns.get_loc("Open")] = 90
    frame.iloc[30:35, frame.columns.get_loc("High")] = 90
    frame.iloc[30:35, frame.columns.get_loc("Low")] = 90

    analysis = analyze_kline_quality(
        frame,
        timeframe=Timeframe(type="day"),
        config=CleaningConfig(mode="filter", min_segment_bars=2),
        required_bars=2,
    )

    assert 12 in analysis.structural_breaks
    assert analysis.reasons["illiquid"] == (20, 21, 22)
    assert set(range(31, 35)).issubset(analysis.reasons["stagnant"])


def test_report_only_preserves_existing_preprocess_result():
    frame = candles(100)
    frame.iloc[50, frame.columns.get_loc("Open")] = frame.iloc[49]["Close"] * 1.5

    off = run_preprocess(frame, preset("off"))
    report = run_preprocess(frame, preset("report_only"))

    assert [(s.start_position, s.end_position, s.label) for s in report.samples] == [
        (s.start_position, s.end_position, s.label) for s in off.samples
    ]
    assert report.points.equals(off.points)
    assert report.stats["cleaning"]["structural_breaks"] >= 1
    assert report.stats["cleaning"]["mode"] == "report_only"


def test_filter_never_builds_sample_across_structural_break():
    frame = candles(120)
    frame.iloc[60, frame.columns.get_loc("Open")] = frame.iloc[59]["Close"] * 1.5
    result = run_preprocess(frame, preset("filter"))

    boundary_time = frame.index[60]
    for sample in result.samples:
        start = result.frame.index[sample.start_position]
        end = result.frame.index[sample.end_position]
        assert not (start < boundary_time <= end)
    assert result.stats["cleaning"]["mode"] == "filter"
    assert result.stats["cleaning"]["structural_breaks"] >= 1
    assert list(result.points.index) == [
        result.frame.index[int(position)] for position in result.points["position"]
    ]


def test_tick_defaults_apply_only_field_integrity_rules():
    frame = candles(20)
    frame.iloc[10, frame.columns.get_loc("Open")] *= 3
    analysis = analyze_kline_quality(
        frame,
        timeframe=Timeframe(type="tick", unit=1),
        config=CleaningConfig(mode="report_only", min_segment_bars=1),
        required_bars=1,
    )
    assert analysis.thresholds["price_jump_threshold"] is None
    assert analysis.structural_breaks == ()


def test_filter_returns_empty_result_when_all_segments_are_too_short():
    frame = candles(20)
    config = preset("filter").model_copy(
        update={"cleaning": CleaningConfig(mode="filter", min_segment_bars=30)}
    )

    result = run_preprocess(frame, config)

    assert result.frame.empty
    assert result.points.empty
    assert result.samples == []
    assert result.stats["cleaning"]["retained_bars"] == 0
