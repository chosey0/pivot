"""시퀀스 샘플 생성 (구 create_dataset의 max_len 고정 윈도우를 대체).

입력 윈도우는 **직전 반대 종류 프랙탈 마커부터 현재 마커까지**의 스윙 구간이다:
고점 샘플은 직전 저점 마커 ~ 해당 고점, 저점 샘플은 직전 고점 마커 ~ 해당 저점
(양 끝 봉 포함, 가변 길이). 직전 반대 마커가 없는 첫 지점은 샘플에서 제외한다.
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
from pivot.config import PreprocessPreset
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
) -> tuple[list[Sample], int, int]:
    """라벨 지점별 시퀀스 샘플 목록과 (NaN 제외, 짝 없음 제외) 수를 반환한다.

    윈도우 = 직전 반대 종류 마커 위치 ~ 현재 마커 위치 (양 끝 포함).
    직전 반대 마커가 없는 지점(시리즈 첫 스윙)은 제외한다.
    선택한 피처에 NaN이 있는 윈도우(예: MA 초기 구간)는 학습 불가로 제외한다.
    points는 position 오름차순이어야 한다 (label_points 반환 순서).
    """
    features = df[feature_columns]
    samples: list[Sample] = []
    dropped_nan = 0
    dropped_unpaired = 0
    last_position: dict[str, int | None] = {"low": None, "high": None}
    for row in points.itertuples():
        kind = str(row.kind)
        end = int(row.position)
        opposite = "high" if kind == "low" else "low"
        start = last_position[opposite]
        last_position[kind] = end
        if start is None or start >= end:
            # 직전 반대 마커가 없거나 같은 봉(고/저 동시 확정)이면 윈도우 불성립
            dropped_unpaired += 1
            continue
        window = features.iloc[start : end + 1]
        if window.isna().any().any():
            dropped_nan += 1
            continue
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
    return samples, dropped_nan, dropped_unpaired


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
    samples, dropped_nan, dropped_unpaired = build_samples(
        enriched, points, preset.features
    )
    plateau = label_stats.pop("plateau")
    sample_overlap = analyze_overlap_clusters(
        samples, max_end_gap=confirmation_lag(preset.fractal.n)
    )

    class_counts = {0: 0, 1: 0, 2: 0}
    for sample in samples:
        class_counts[sample.label] += 1
    stats = {
        "bars": len(df),
        "points": len(points),
        "samples": len(samples),
        "class_counts": class_counts,
        "dropped_nan": dropped_nan,
        "dropped_unpaired": dropped_unpaired,
        **label_stats,
        "overlap_clusters": _combined_overlap_stats(plateau, sample_overlap),
        "confirmation_lag": confirmation_lag(preset.fractal.n),
    }
    return PreprocessResult(
        frame=enriched,
        points=points,
        samples=samples,
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
    }
    overlap_totals = _empty_overlap_stats(preset)
    class_counts = {0: 0, 1: 0, 2: 0}
    offset = 0
    for segment in segments:
        part = _run_segment(df.iloc[segment.start : segment.end + 1].copy(), preset)
        frames.append(part.frame)
        adjusted_points = part.points.copy()
        adjusted_points["position"] = adjusted_points["position"] + offset
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
