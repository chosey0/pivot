"""윌리엄스 프랙탈 라벨링 검증. 창 정렬·확정 lag·라벨 규약을 고정한다."""

import numpy as np
import pandas as pd
import pytest

from pivot.config import FilterConfig, FractalConfig, LabelingConfig, PreprocessPreset
from pivot.dataset.build import build_samples, run_preprocess
from pivot.labeling.fractal import calc_fractal, confirmation_lag, label_points


def make_df(highs, lows=None, amount=1_000_000_000) -> pd.DataFrame:
    highs = np.asarray(highs, dtype=float)
    lows = np.asarray(lows, dtype=float) if lows is not None else highs - 1.0
    index = pd.date_range("2026-01-01", periods=len(highs), freq="D", name="Time")
    close = (highs + lows) / 2
    return pd.DataFrame(
        {
            "Open": close,
            "High": highs,
            "Low": lows,
            "Close": close,
            "Volume": 1000,
            "Amount": amount,
        },
        index=index,
    )


def ramp(length: int, start=100.0, stop=110.0) -> np.ndarray:
    """tie가 생기지 않는 완만한 단조 시리즈."""
    return np.linspace(start, stop, length)


class TestCalcFractal:
    def test_center_peak_is_marked(self):
        # n=5: 과거 2 + 중심 + 미래 2. 위치 10에 유일한 고점 스파이크.
        highs = ramp(21)
        highs[10] += 50
        df = calc_fractal(make_df(highs), n=5)
        assert list(df["fractal_high"].dropna().index) == [df.index[10]]

    def test_low_is_marked(self):
        highs = ramp(21)
        lows = highs - 1
        lows[10] -= 50
        df = calc_fractal(make_df(highs, lows), n=5)
        assert list(df["fractal_low"].dropna().index) == [df.index[10]]

    def test_tail_lag_bars_never_confirmed(self):
        # 마지막 (n-1)//2봉은 미래 확인 봉 부족 → 스파이크여도 라벨 금지.
        n = 21
        lag = confirmation_lag(n)  # 10
        highs = ramp(60)
        highs[-3] += 50  # lag 구간 안의 스파이크
        df = calc_fractal(make_df(highs), n=n)
        assert df["fractal_high"].iloc[-lag:].isna().all()

    def test_head_bars_without_past_window_not_confirmed(self):
        n = 20
        past = n // 2
        highs = ramp(60)
        highs[3] += 50  # 과거 창 부족 구간의 스파이크
        df = calc_fractal(make_df(highs), n=n)
        assert df["fractal_high"].iloc[:past].isna().all()

    def test_window_alignment_matches_pandas_center_rolling(self):
        # 구 파이프라인(pandas center rolling)과 같은 정렬인지 무작위 시리즈로 확인.
        rng = np.random.default_rng(7)
        highs = rng.uniform(10, 20, 200)
        for n in (5, 20, 21):
            df = calc_fractal(make_df(highs), n=n)
            series = pd.Series(highs)
            expected = series == series.rolling(n, center=True, min_periods=n).max()
            assert (
                df["fractal_high"].notna().to_numpy() == expected.to_numpy()
            ).all(), f"n={n}"

    def test_n_must_be_at_least_3(self):
        with pytest.raises(ValueError):
            calc_fractal(make_df(ramp(10)), n=2)


class TestLabelPoints:
    def make_trend_df(self, invert=False):
        # 완만한 추세 + 위치 130 고점 / 140 저점 스파이크 (MA120 확보 구간).
        base = ramp(200, 100, 140)
        if invert:
            base = base[::-1].copy()
        highs = base.copy()
        highs[130] += 50
        lows = base - 2
        lows[140] -= 50
        df = make_df(highs, lows)
        for w in (5, 20, 120):
            df[str(w)] = df["Close"].rolling(w).mean()
        return df

    def test_labels_follow_convention(self):
        # 상승 추세(정배열): 저점=0, 고점=1.
        df = self.make_trend_df()
        points, _ = label_points(df, n=5)
        by_pos = {int(row.position): int(row.label) for row in points.itertuples()}
        assert by_pos[130] == 1  # fractal high
        assert by_pos[140] == 0  # fractal low

    def test_ignore_rule_overrides_to_2(self):
        # 하락 추세(역배열, MA20 < MA120): 고점/저점 모두 2로 덮어씀.
        df = self.make_trend_df(invert=True)
        points, _ = label_points(df, n=5, labeling=LabelingConfig(mode="cls3"))
        assert not points.empty
        assert (points["label"] == 2).all()

    def test_cls2_drop_removes_ignored(self):
        df = self.make_trend_df(invert=True)
        points, stats = label_points(df, n=5, labeling=LabelingConfig(mode="cls2_drop"))
        assert (points["label"] != 2).all() if not points.empty else True
        assert stats["dropped_ignore"] > 0

    def test_min_amount_filter_drops_points(self):
        df = self.make_trend_df()
        all_points, _ = label_points(df, n=5, labeling=LabelingConfig(ignore_rule="none"))
        filtered, stats = label_points(
            df,
            n=5,
            labeling=LabelingConfig(ignore_rule="none"),
            filters=FilterConfig(min_amount=2_000_000_000),  # amount=1e9 → 전부 탈락
        )
        assert len(all_points) > 0
        assert filtered.empty
        assert stats["dropped_filters"] == len(all_points)

    def test_bar_that_is_both_high_and_low_yields_two_points(self):
        highs = ramp(21)
        lows = highs - 1
        highs[10] += 50
        lows[10] -= 50
        df = make_df(highs, lows)
        points, _ = label_points(df, n=5, labeling=LabelingConfig(ignore_rule="none"))
        assert sorted(points["kind"]) == ["high", "low"]

    def test_plateau_last_keeps_only_last_equal_extreme(self):
        highs = ramp(30)
        lows = highs - 1
        lows[10:13] = 0
        df = make_df(highs, lows)

        all_points, all_stats = label_points(
            df,
            n=5,
            tie_policy="all",
            labeling=LabelingConfig(ignore_rule="none"),
        )
        normalized, stats = label_points(
            df,
            n=5,
            tie_policy="plateau_last",
            labeling=LabelingConfig(ignore_rule="none"),
        )

        assert list(all_points.loc[all_points["kind"] == "low", "position"]) == [10, 11, 12]
        assert list(normalized.loc[normalized["kind"] == "low", "position"]) == [12]
        assert all_stats["plateau"]["dropped_points"] == 0
        assert stats["plateau"] == {
            "tie_policy": "plateau_last",
            "candidate_points": 3,
            "retained_points": 1,
            "clusters": 1,
            "clustered_points": 3,
            "dropped_points": 2,
            "max_cluster_size": 3,
        }

    def test_new_preprocess_presets_default_to_plateau_last(self):
        assert FractalConfig().tie_policy == "plateau_last"


class TestBuildSamples:
    def make_swing_df(self, length: int, highs_at=(), lows_at=()) -> pd.DataFrame:
        """완만한 ramp 위에 지정 위치의 고점/저점 스파이크를 얹은 시리즈."""
        highs = ramp(length)
        lows = highs - 1
        for position in highs_at:
            highs[position] += 50
        for position in lows_at:
            lows[position] -= 50
        return make_df(highs, lows)

    def test_window_spans_from_previous_opposite_marker(self):
        # 고점@30 → 저점@50: 저점 샘플의 윈도우 = 직전 고점(30)부터 저점(50)까지.
        df = self.make_swing_df(80, highs_at=(30,), lows_at=(50,))
        points, _ = label_points(df, n=5, labeling=LabelingConfig(ignore_rule="none"))
        samples, dropped_nan, dropped_unpaired = build_samples(
            df, points, feature_columns=["Open", "High", "Low", "Close"]
        )
        assert dropped_nan == 0
        assert dropped_unpaired == 1  # 첫 고점은 직전 저점 마커가 없음
        [sample] = samples
        assert sample.kind == "low"
        assert sample.start_position == 30
        assert sample.end_position == 50
        assert sample.length == 21

    def test_alternating_swings_pair_with_latest_opposite(self):
        # 고점@20 → 저점@35 → 고점@55: 저점은 20부터, 마지막 고점은 35부터.
        df = self.make_swing_df(90, highs_at=(20, 55), lows_at=(35,))
        points, _ = label_points(df, n=5, labeling=LabelingConfig(ignore_rule="none"))
        samples, _, dropped_unpaired = build_samples(
            df, points, feature_columns=["Close"]
        )
        assert dropped_unpaired == 1
        windows = {(s.kind, s.start_position, s.end_position) for s in samples}
        assert windows == {("low", 20, 35), ("high", 35, 55)}

    def test_first_point_without_opposite_marker_is_dropped(self):
        df = self.make_swing_df(40, highs_at=(20,))
        points, _ = label_points(df, n=5, labeling=LabelingConfig(ignore_rule="none"))
        assert len(points) == 1
        samples, _, dropped_unpaired = build_samples(
            df, points, feature_columns=["Close"]
        )
        assert samples == []
        assert dropped_unpaired == 1

    def test_nan_feature_window_is_dropped(self):
        # MA20 확보 전 구간을 포함한 윈도우는 피처 NaN → 샘플 제외.
        df = self.make_swing_df(60, highs_at=(10,), lows_at=(25,))
        df["20"] = df["Close"].rolling(20).mean()
        points, _ = label_points(df, n=5, labeling=LabelingConfig(ignore_rule="none"))
        assert len(points) == 2
        samples, dropped_nan, dropped_unpaired = build_samples(
            df, points, feature_columns=["Close", "20"]
        )
        assert samples == []
        assert dropped_unpaired == 1  # 고점@10
        assert dropped_nan == 1  # 저점@25 윈도우 [10, 25]에 MA20 NaN 포함


class TestRunPreprocess:
    def test_end_to_end_stats(self):
        rng = np.random.default_rng(11)
        close = 100 + np.cumsum(rng.normal(0, 1, 400))
        highs = close + rng.uniform(0.5, 2, 400)
        lows = close - rng.uniform(0.5, 2, 400)
        df = make_df(highs, lows)
        preset = PreprocessPreset(name="test")
        result = run_preprocess(df, preset)
        stats = result.stats
        assert stats["bars"] == 400
        assert stats["samples"] == sum(stats["class_counts"].values())
        assert (
            stats["points"]
            == stats["samples"] + stats["dropped_nan"] + stats["dropped_unpaired"]
        )
        assert stats["confirmation_lag"] == confirmation_lag(20)
        assert result.feature_columns == ["Open", "High", "Low", "Close", "20", "120"]
        # 마지막 lag봉에는 라벨 지점이 없어야 한다 (미확정 구간).
        assert (result.points["position"] < 400 - confirmation_lag(20)).all()
