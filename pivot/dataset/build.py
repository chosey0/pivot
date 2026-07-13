"""시퀀스 샘플 생성 (구 create_dataset의 max_len 고정 윈도우를 대체).

입력 윈도우는 프리셋의 pairing 전략이 선택한 두 프랙탈 마커 사이의 가변 길이
구간이다. 신규 기본값은 시간순 인접 마커, legacy fallback은 최근 반대 마커이며
양 끝 봉을 모두 포함한다.
구 방식과 달리 (백로그 A그룹):
- low/high 루프를 label_points로 통합 (A7)
- 피처는 float로 유지, int64 캐스팅 없음 (A1)
- Time은 피처에 넣지 않고 윈도우 범위 메타로만 유지 (A2)

Lab 단건 preview와 M3 일괄 batch가 모두 `run_preprocess`를 호출한다 —
호출자별 파이프라인 복제 금지 (docs/05 설계 원칙).
"""

from dataclasses import dataclass, field

import pandas as pd

from pivot.cleaning.kronos import (
    PAPER_URL,
    POLICY_VERSION,
    CleanSegment,
    analyze_kline_quality,
)
from pivot.config import LabelingConfig, PreprocessPreset
from pivot.ingestion.indicators import add_moving_averages
from pivot.labeling.fractal import confirmation_lag, label_points
from pivot.dataset.overlap import analyze_overlap_clusters


@dataclass
class Sample:
    """모델 입력 시퀀스 하나. position은 원본 DataFrame의 iloc 위치."""

    end_position: int
    start_position: int
    label: int
    kind: str  # low | high
    price: float
    length: int


@dataclass
class SampleBuildResult:
    samples: list[Sample]
    dropped_nan: int
    dropped_unpaired: int
    dropped_ignore: int
    swing_ignored: int
    pairing_stats: dict
    incoming: list[dict]


@dataclass
class PreprocessResult:
    """단건 전처리 결과. points는 차트 마커용, samples는 학습 시퀀스용.

    frame은 이평선 컬럼까지 계산된 표준 DataFrame — preview 응답의 캔들/MA
    직렬화가 라벨링과 같은 값을 쓰도록 함께 반환한다.
    """

    frame: pd.DataFrame
    points: pd.DataFrame  # label_points 반환 (필터 통과한 라벨 지점 전체)
    samples: list[Sample]
    feature_columns: list[str]
    stats: dict = field(default_factory=dict)


def build_samples(
    df: pd.DataFrame,
    points: pd.DataFrame,
    feature_columns: list[str],
    labeling: LabelingConfig | None = None,
) -> SampleBuildResult:
    """설정된 pairing 전략으로 라벨 지점 사이의 시퀀스 샘플을 만든다."""
    labeling = labeling or LabelingConfig()
    if labeling.sample_pairing == "adjacent_markers_v1":
        return _build_adjacent_samples(df, points, feature_columns, labeling)
    return _build_latest_opposite_samples(df, points, feature_columns)


def _build_latest_opposite_samples(
    df: pd.DataFrame, points: pd.DataFrame, feature_columns: list[str]
) -> SampleBuildResult:
    features = df[feature_columns]
    samples: list[Sample] = []
    dropped_nan = 0
    dropped_unpaired = 0
    incoming: list[dict] = []
    last_position: dict[str, int | None] = {"low": None, "high": None}
    for row in points.itertuples():
        kind = str(row.kind)
        end = int(row.position)
        opposite = "high" if kind == "low" else "low"
        start = last_position[opposite]
        last_position[kind] = end
        if start is None or start >= end:
            dropped_unpaired += 1
            incoming.append(_incoming(None, False, None, "unpaired"))
            continue
        window = features.iloc[start : end + 1]
        if window.isna().any().any():
            dropped_nan += 1
            incoming.append(_incoming(int(row.label), False, None, "nan"))
            continue
        sample_index = len(samples)
        samples.append(
            Sample(
                end_position=end,
                start_position=start,
                label=int(row.label),
                kind=kind,
                price=float(row.price),
                length=end - start + 1,
            )
        )
        incoming.append(_incoming(int(row.label), True, sample_index, None))
    paired = len(points) - dropped_unpaired
    return SampleBuildResult(
        samples=samples,
        dropped_nan=dropped_nan,
        dropped_unpaired=dropped_unpaired,
        dropped_ignore=0,
        swing_ignored=0,
        pairing_stats={
            "rule": "latest_opposite_v1",
            "adjacent_edges": paired,
            "unpaired_markers": dropped_unpaired,
            "dropped_invalid_position": 0,
            "dropped_label2": 0,
        },
        incoming=incoming,
    )


def _build_adjacent_samples(
    df: pd.DataFrame,
    points: pd.DataFrame,
    feature_columns: list[str],
    labeling: LabelingConfig,
) -> SampleBuildResult:
    features = df[feature_columns]
    samples: list[Sample] = []
    incoming: list[dict] = []
    dropped_nan = 0
    dropped_invalid = 0
    dropped_label2 = 0
    swing_ignored = 0
    previous = None

    for row in points.itertuples():
        if previous is None:
            incoming.append(_incoming(None, False, None, "unpaired"))
            previous = row
            continue

        start = int(previous.position)
        end = int(row.position)
        same_kind = str(previous.kind) == str(row.kind)
        label = 2 if same_kind else int(row.label)
        if start >= end:
            dropped_invalid += 1
            incoming.append(_incoming(label, False, None, "invalid_position"))
            previous = row
            continue

        if label != 2 and labeling.ignore_swing_pct is not None:
            start_price = float(previous.price)
            end_price = float(row.price)
            if (
                start_price > 0
                and abs(end_price / start_price - 1.0) * 100.0
                < labeling.ignore_swing_pct
            ):
                label = 2
                swing_ignored += 1

        if label == 2 and labeling.mode == "cls2_drop":
            dropped_label2 += 1
            incoming.append(_incoming(label, False, None, "label2"))
            previous = row
            continue

        window = features.iloc[start : end + 1]
        if window.isna().any().any():
            dropped_nan += 1
            incoming.append(_incoming(label, False, None, "nan"))
            previous = row
            continue

        sample_index = len(samples)
        samples.append(
            Sample(
                end_position=end,
                start_position=start,
                label=label,
                kind=str(row.kind),
                price=float(row.price),
                length=end - start + 1,
            )
        )
        incoming.append(_incoming(label, True, sample_index, None))
        previous = row

    unpaired = 1 if len(points) else 0
    return SampleBuildResult(
        samples=samples,
        dropped_nan=dropped_nan,
        dropped_unpaired=unpaired,
        dropped_ignore=dropped_label2,
        swing_ignored=swing_ignored,
        pairing_stats={
            "rule": "adjacent_markers_v1",
            "adjacent_edges": max(len(points) - 1, 0),
            "unpaired_markers": unpaired,
            "dropped_invalid_position": dropped_invalid,
            "dropped_label2": dropped_label2,
        },
        incoming=incoming,
    )


def _incoming(
    label: int | None, included: bool, index: int | None, reason: str | None
) -> dict:
    return {
        "incoming_sample_label": label,
        "incoming_sample_included": included,
        "incoming_sample_index": index,
        "incoming_sample_drop_reason": reason,
    }


def run_preprocess(df: pd.DataFrame, preset: PreprocessPreset) -> PreprocessResult:
    """표준 캔들 DataFrame에 프리셋을 적용해 라벨 지점 + 샘플 + 통계를 만든다.

    df는 ingestion 캐시 로드 결과 (Time 인덱스 + OHLCV/Amount). 이평선은
    ignore 규칙·필터·피처에 필요한 기간을 여기서 일괄 계산한다.
    """
    required_bars = max([preset.fractal.n, *preset.required_ma_windows], default=1)
    cleaning = preset.cleaning
    if cleaning.mode == "off":
        result = _run_segment(df, preset)
        result.stats["cleaning"] = {
            "mode": "off",
            "policy": POLICY_VERSION,
            "reference": PAPER_URL,
            "original_bars": len(df),
            "retained_bars": len(df),
            "removed_bars": 0,
            "removed_ratio": 0.0,
            "segments": 1 if len(df) else 0,
            "segment_lengths": [len(df)] if len(df) else [],
            "structural_breaks": 0,
            "reason_counts": {},
            "thresholds": {},
        }
        return result

    analysis = analyze_kline_quality(
        df,
        timeframe=preset.timeframe,
        config=cleaning,
        required_bars=required_bars,
    )
    if cleaning.mode == "report_only":
        result = _run_segment(df, preset)
        result.stats["cleaning"] = {"mode": "report_only", **analysis.to_stats()}
        return result

    result = _run_clean_segments(df, preset, analysis.segments)
    result.stats["cleaning"] = {"mode": "filter", **analysis.to_stats()}
    return result


def _run_segment(df: pd.DataFrame, preset: PreprocessPreset) -> PreprocessResult:
    """연속된 단일 구간을 전처리한다."""
    enriched = add_moving_averages(df, windows=preset.required_ma_windows)
    points, label_stats = label_points(
        enriched,
        n=preset.fractal.n,
        tie_policy=preset.fractal.tie_policy,
        labeling=preset.labeling,
        filters=preset.filters,
    )
    built = build_samples(enriched, points, preset.features, preset.labeling)
    points = points.copy()
    for key in (
        "incoming_sample_label",
        "incoming_sample_included",
        "incoming_sample_index",
        "incoming_sample_drop_reason",
    ):
        points[key] = [item[key] for item in built.incoming]
    plateau = label_stats.pop("plateau")
    sample_overlap = analyze_overlap_clusters(
        built.samples, max_end_gap=confirmation_lag(preset.fractal.n)
    )

    class_counts = {0: 0, 1: 0, 2: 0}
    for sample in built.samples:
        class_counts[sample.label] += 1
    label_stats["dropped_ignore"] += built.dropped_ignore
    label_stats["swing_ignored"] += built.swing_ignored
    stats = {
        "bars": len(df),
        "points": len(points),
        "samples": len(built.samples),
        "class_counts": class_counts,
        "dropped_nan": built.dropped_nan,
        "dropped_unpaired": built.dropped_unpaired,
        **label_stats,
        "pairing_stats": built.pairing_stats,
        "overlap_clusters": _combined_overlap_stats(plateau, sample_overlap),
        "confirmation_lag": confirmation_lag(preset.fractal.n),
    }
    return PreprocessResult(
        frame=enriched,
        points=points,
        samples=built.samples,
        feature_columns=list(preset.features),
        stats=stats,
    )


def _run_clean_segments(
    df: pd.DataFrame,
    preset: PreprocessPreset,
    segments: tuple[CleanSegment, ...],
) -> PreprocessResult:
    """정상 세그먼트별로 지표·라벨·샘플을 독립 계산해 결합한다."""
    frames: list[pd.DataFrame] = []
    point_frames: list[pd.DataFrame] = []
    samples: list[Sample] = []
    totals = {
        "points": 0,
        "samples": 0,
        "dropped_nan": 0,
        "dropped_unpaired": 0,
        "dropped_filters": 0,
        "dropped_ignore": 0,
        "swing_ignored": 0,
    }
    pairing_totals = _empty_pairing_stats(preset)
    overlap_totals = _empty_overlap_stats(preset)
    class_counts = {0: 0, 1: 0, 2: 0}
    offset = 0
    for segment in segments:
        part = _run_segment(df.iloc[segment.start : segment.end + 1].copy(), preset)
        frames.append(part.frame)
        adjusted_points = part.points.copy()
        adjusted_points["position"] = adjusted_points["position"] + offset
        sample_offset = len(samples)
        sample_index = adjusted_points["incoming_sample_index"]
        included = sample_index.notna()
        adjusted_points.loc[included, "incoming_sample_index"] = (
            sample_index.loc[included].astype(int) + sample_offset
        )
        point_frames.append(adjusted_points)
        samples.extend(
            Sample(
                end_position=sample.end_position + offset,
                start_position=sample.start_position + offset,
                label=sample.label,
                kind=sample.kind,
                price=sample.price,
                length=sample.length,
            )
            for sample in part.samples
        )
        for key in totals:
            totals[key] += int(part.stats[key])
        for label, count in part.stats["class_counts"].items():
            class_counts[int(label)] += int(count)
        _merge_pairing_stats(pairing_totals, part.stats["pairing_stats"])
        _merge_overlap_stats(overlap_totals, part.stats["overlap_clusters"])
        offset += len(part.frame)

    if frames:
        frame = pd.concat(frames)
    else:
        frame = add_moving_averages(
            df.iloc[0:0].copy(), windows=preset.required_ma_windows
        )
    if point_frames:
        points = pd.concat(point_frames)
    else:
        points = label_points(
            frame,
            n=preset.fractal.n,
            tie_policy=preset.fractal.tie_policy,
            labeling=preset.labeling,
            filters=preset.filters,
        )[0]
    return PreprocessResult(
        frame=frame,
        points=points,
        samples=samples,
        feature_columns=list(preset.features),
        stats={
            "bars": len(frame),
            **totals,
            "class_counts": class_counts,
            "pairing_stats": pairing_totals,
            "overlap_clusters": overlap_totals,
            "confirmation_lag": confirmation_lag(preset.fractal.n),
        },
    )


def _combined_overlap_stats(plateau: dict, samples: dict) -> dict:
    return {
        "tie_policy": plateau["tie_policy"],
        "plateau_clusters": plateau["clusters"],
        "plateau_clustered_points": plateau["clustered_points"],
        "dropped_plateau_points": plateau["dropped_points"],
        "max_plateau_cluster_size": plateau["max_cluster_size"],
        "sample_clusters": samples["clusters"],
        "clustered_samples": samples["clustered_samples"],
        "redundant_samples": samples["redundant_samples"],
        "max_sample_cluster_size": samples["max_cluster_size"],
        "threshold": samples["threshold"],
        "max_end_gap": samples["max_end_gap"],
    }


def _empty_pairing_stats(preset: PreprocessPreset) -> dict:
    return {
        "rule": preset.labeling.sample_pairing,
        "adjacent_edges": 0,
        "unpaired_markers": 0,
        "dropped_invalid_position": 0,
        "dropped_label2": 0,
    }


def _merge_pairing_stats(total: dict, part: dict) -> None:
    if part["rule"] != total["rule"]:
        raise ValueError("pairing rule changed between cleaning segments")
    for key in (
        "adjacent_edges",
        "unpaired_markers",
        "dropped_invalid_position",
        "dropped_label2",
    ):
        total[key] += int(part[key])


def _empty_overlap_stats(preset: PreprocessPreset) -> dict:
    return {
        "tie_policy": preset.fractal.tie_policy,
        "plateau_clusters": 0,
        "plateau_clustered_points": 0,
        "dropped_plateau_points": 0,
        "max_plateau_cluster_size": 0,
        "sample_clusters": 0,
        "clustered_samples": 0,
        "redundant_samples": 0,
        "max_sample_cluster_size": 0,
        "threshold": 0.9,
        "max_end_gap": confirmation_lag(preset.fractal.n),
    }


def _merge_overlap_stats(total: dict, part: dict) -> None:
    for key in (
        "plateau_clusters",
        "plateau_clustered_points",
        "dropped_plateau_points",
        "sample_clusters",
        "clustered_samples",
        "redundant_samples",
    ):
        total[key] += int(part[key])
    total["max_plateau_cluster_size"] = max(
        total["max_plateau_cluster_size"], int(part["max_plateau_cluster_size"])
    )
    total["max_sample_cluster_size"] = max(
        total["max_sample_cluster_size"], int(part["max_sample_cluster_size"])
    )
