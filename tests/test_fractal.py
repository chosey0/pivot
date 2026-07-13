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
        points, stats = label_points(
            df,
            n=5,
            labeling=LabelingConfig(
                mode="cls2_drop", sample_pairing="latest_opposite_v1"
            ),
        )
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
        built = build_samples(
            df,
            points,
            feature_columns=["Open", "High", "Low", "Close"],
            labeling=LabelingConfig(
                ignore_rule="none", sample_pairing="latest_opposite_v1"
            ),
        )
        assert built.dropped_nan == 0
        assert built.dropped_unpaired == 1  # 첫 고점은 직전 저점 마커가 없음
        [sample] = built.samples
        assert sample.kind == "low"
        assert sample.start_position == 30
        assert sample.end_position == 50
        assert sample.length == 21

    def test_alternating_swings_pair_with_latest_opposite(self):
        # 고점@20 → 저점@35 → 고점@55: 저점은 20부터, 마지막 고점은 35부터.
        df = self.make_swing_df(90, highs_at=(20, 55), lows_at=(35,))
        points, _ = label_points(df, n=5, labeling=LabelingConfig(ignore_rule="none"))
        built = build_samples(
            df,
            points,
            feature_columns=["Close"],
            labeling=LabelingConfig(
                ignore_rule="none", sample_pairing="latest_opposite_v1"
            ),
        )
        assert built.dropped_unpaired == 1
        windows = {(s.kind, s.start_position, s.end_position) for s in built.samples}
        assert windows == {("low", 20, 35), ("high", 35, 55)}

    def test_first_point_without_opposite_marker_is_dropped(self):
        df = self.make_swing_df(40, highs_at=(20,))
        points, _ = label_points(df, n=5, labeling=LabelingConfig(ignore_rule="none"))
        assert len(points) == 1
        built = build_samples(
            df,
            points,
            feature_columns=["Close"],
            labeling=LabelingConfig(
                ignore_rule="none", sample_pairing="latest_opposite_v1"
            ),
        )
        assert built.samples == []
        assert built.dropped_unpaired == 1

    def test_nan_feature_window_is_dropped(self):
        # MA20 확보 전 구간을 포함한 윈도우는 피처 NaN → 샘플 제외.
        df = self.make_swing_df(60, highs_at=(10,), lows_at=(25,))
        df["20"] = df["Close"].rolling(20).mean()
        points, _ = label_points(df, n=5, labeling=LabelingConfig(ignore_rule="none"))
        assert len(points) == 2
        built = build_samples(
            df,
            points,
            feature_columns=["Close", "20"],
            labeling=LabelingConfig(
                ignore_rule="none", sample_pairing="latest_opposite_v1"
            ),
        )
        assert built.samples == []
        assert built.dropped_unpaired == 1  # 고점@10
        assert built.dropped_nan == 1  # 저점@25 윈도우 [10, 25]에 MA20 NaN 포함

    def test_adjacent_pairing_labels_same_kind_and_keeps_next_anchor(self):
        df = self.make_swing_df(50)
        points = pd.DataFrame(
            [
                {"position": 10, "kind": "low", "price": 90.0, "label": 0},
                {"position": 20, "kind": "low", "price": 91.0, "label": 0},
                {"position": 30, "kind": "high", "price": 110.0, "label": 1},
            ],
            index=df.index[[10, 20, 30]],
        )

        built = build_samples(
            df,
            points,
            ["Close"],
            labeling=LabelingConfig(ignore_rule="none"),
        )

        assert [sample.label for sample in built.samples] == [2, 1]
        assert [
            (sample.start_position, sample.end_position) for sample in built.samples
        ] == [(10, 20), (20, 30)]
        assert built.pairing_stats == {
            "rule": "adjacent_markers_v1",
            "adjacent_edges": 2,
            "unpaired_markers": 1,
            "dropped_invalid_position": 0,
            "dropped_label2": 0,
        }

    def test_adjacent_cls2_drops_label2_sample_but_keeps_marker_anchor(self):
        df = self.make_swing_df(50)
        points = pd.DataFrame(
            [
                {"position": 10, "kind": "low", "price": 90.0, "label": 0},
                {"position": 20, "kind": "low", "price": 91.0, "label": 0},
                {"position": 30, "kind": "high", "price": 110.0, "label": 1},
            ],
            index=df.index[[10, 20, 30]],
        )

        built = build_samples(
            df,
            points,
            ["Close"],
            labeling=LabelingConfig(mode="cls2_drop", ignore_rule="none"),
        )

        assert [(sample.start_position, sample.end_position) for sample in built.samples] == [
            (20, 30)
        ]
        assert built.dropped_ignore == 1
        assert built.pairing_stats["dropped_label2"] == 1
        assert built.incoming[1] == {
            "incoming_sample_label": 2,
            "incoming_sample_included": False,
            "incoming_sample_index": None,
            "incoming_sample_drop_reason": "label2",
        }
        assert built.incoming[2]["incoming_sample_index"] == 0

    def test_adjacent_pairing_counts_same_position_as_invalid(self):
        df = self.make_swing_df(40)
        points = pd.DataFrame(
            [
                {"position": 10, "kind": "high", "price": 110.0, "label": 1},
                {"position": 10, "kind": "low", "price": 90.0, "label": 0},
                {"position": 20, "kind": "high", "price": 120.0, "label": 1},
            ],
            index=df.index[[10, 10, 20]],
        )

        built = build_samples(df, points, ["Close"], labeling=LabelingConfig(ignore_rule="none"))

        assert [(sample.start_position, sample.end_position) for sample in built.samples] == [
            (10, 20)
        ]
        assert built.pairing_stats["dropped_invalid_position"] == 1

    def test_latest_opposite_pairing_preserves_legacy_windows(self):
        df = self.make_swing_df(50)
        points = pd.DataFrame(
            [
                {"position": 10, "kind": "low", "price": 90.0, "label": 0},
                {"position": 20, "kind": "low", "price": 91.0, "label": 0},
                {"position": 30, "kind": "high", "price": 110.0, "label": 1},
            ],
            index=df.index[[10, 20, 30]],
        )

        built = build_samples(
            df,
            points,
            ["Close"],
            labeling=LabelingConfig(
                ignore_rule="none", sample_pairing="latest_opposite_v1"
            ),
        )

        assert [(sample.start_position, sample.end_position) for sample in built.samples] == [
            (20, 30)
        ]
        assert built.dropped_unpaired == 2


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
        pairing = stats["pairing_stats"]
        assert stats["points"] == pairing["adjacent_edges"] + pairing["unpaired_markers"]
        assert pairing["adjacent_edges"] == (
            stats["samples"]
            + pairing["dropped_label2"]
            + stats["dropped_nan"]
            + pairing["dropped_invalid_position"]
        )
        assert stats["confirmation_lag"] == confirmation_lag(20)
        assert result.feature_columns == ["Open", "High", "Low", "Close", "20", "120"]
        # 마지막 lag봉에는 라벨 지점이 없어야 한다 (미확정 구간).
        assert (result.points["position"] < 400 - confirmation_lag(20)).all()


class TestSwingIgnoreRule:
    """스윙 진폭 무시 규칙 (labeling.ignore_swing_pct)."""

    def make_zigzag(self):
        """저점@5 → 고점@10(작은 스윙) → 저점@15 → 고점@20(큰 스윙)."""
        highs = ramp(30)
        lows = highs - 1.0
        lows[5] -= 1.0
        highs[10] += 1.0
        lows[15] = 80.0
        highs[20] = 120.0
        small_swing_pct = (highs[10] / lows[5] - 1.0) * 100.0
        return make_df(highs, lows), small_swing_pct

    def test_small_swing_relabeled_to_ignore_in_cls3(self):
        df, small = self.make_zigzag()
        points, stats = label_points(
            df,
            n=5,
            labeling=LabelingConfig(
                mode="cls3",
                ignore_rule="none",
                ignore_swing_pct=small + 1.0,
                sample_pairing="latest_opposite_v1",
            ),
        )
        assert list(points["position"]) == [5, 10, 15, 20]
        # 첫 저점은 직전 반대 프랙탈이 없어 규칙 미적용, 작은 스윙 고점만 무시(2)
        assert list(points["label"]) == [0, 2, 0, 1]
        assert stats["swing_ignored"] == 1

    def test_threshold_below_swing_keeps_labels(self):
        df, small = self.make_zigzag()
        points, stats = label_points(
            df,
            n=5,
            labeling=LabelingConfig(
                mode="cls3",
                ignore_rule="none",
                ignore_swing_pct=small - 1.0,
                sample_pairing="latest_opposite_v1",
            ),
        )
        assert list(points["label"]) == [0, 1, 0, 1]
        assert stats["swing_ignored"] == 0

    def test_cls2_drop_removes_small_swing_and_anchor(self):
        df, small = self.make_zigzag()
        points, stats = label_points(
            df,
            n=5,
            labeling=LabelingConfig(
                mode="cls2_drop",
                ignore_rule="none",
                ignore_swing_pct=small + 1.0,
                sample_pairing="latest_opposite_v1",
            ),
        )
        # 작은 스윙 고점@10은 제거되고, 제거된 지점은 다음 스윙의 anchor가 아니다
        assert list(points["position"]) == [5, 15, 20]
        assert list(points["label"]) == [0, 0, 1]
        assert stats["dropped_ignore"] == 1

    def test_disabled_by_default(self):
        df, _ = self.make_zigzag()
        points, stats = label_points(
            df, n=5, labeling=LabelingConfig(ignore_rule="none")
        )
        assert list(points["label"]) == [0, 1, 0, 1]
        assert stats["swing_ignored"] == 0

    def test_run_preprocess_carries_swing_stat_and_sample_labels(self):
        df, small = self.make_zigzag()
        preset = PreprocessPreset(
            name="swing",
            fractal=FractalConfig(n=5),
            features=["Open", "High", "Low", "Close"],
            labeling=LabelingConfig(ignore_rule="none", ignore_swing_pct=small + 1.0),
        )
        result = run_preprocess(df, preset)
        assert result.stats["swing_ignored"] == 1
        # 무시로 재라벨된 지점도 샘플로는 유지된다 (cls3)
        assert [sample.label for sample in result.samples] == [2, 0, 1]

    def test_ignore_swing_pct_must_be_positive(self):
        with pytest.raises(ValueError):
            LabelingConfig(ignore_swing_pct=0)
