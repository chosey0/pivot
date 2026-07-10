"""시퀀스 샘플 생성 (구 create_dataset 재구현, docs/01 §4).

라벨 지점마다 그 봉을 끝으로 하는 최대 `max_len`봉 윈도우가 모델 입력 시퀀스다.
구 방식과 달리 (백로그 A그룹):
- low/high 루프를 label_points로 통합 (A7)
- 피처는 float로 유지, int64 캐스팅 없음 (A1)
- Time은 피처에 넣지 않고 윈도우 범위 메타로만 유지 (A2)

Lab 단건 preview와 M3 일괄 batch가 모두 `run_preprocess`를 호출한다 —
호출자별 파이프라인 복제 금지 (docs/05 설계 원칙).
"""

from dataclasses import dataclass, field

import pandas as pd

from pivot.config import PreprocessPreset
from pivot.ingestion.indicators import add_moving_averages
from pivot.labeling.fractal import confirmation_lag, label_points


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
    max_len: int,
    feature_columns: list[str],
) -> tuple[list[Sample], int]:
    """라벨 지점별 시퀀스 샘플 목록과 NaN 제외 수를 반환한다.

    윈도우는 라벨 봉을 포함해 뒤에서 max_len봉 (시작부가 짧으면 가변 길이).
    선택한 피처에 NaN이 있는 윈도우(예: MA 초기 구간)는 학습 불가로 제외한다.
    """
    features = df[feature_columns]
    samples: list[Sample] = []
    dropped_nan = 0
    for row in points.itertuples():
        end = int(row.position)
        start = max(0, end - max_len + 1)
        window = features.iloc[start : end + 1]
        if window.isna().any().any():
            dropped_nan += 1
            continue
        samples.append(
            Sample(
                end_position=end,
                start_position=start,
                label=int(row.label),
                kind=str(row.kind),
                price=float(row.price),
                length=end - start + 1,
            )
        )
    return samples, dropped_nan


def run_preprocess(df: pd.DataFrame, preset: PreprocessPreset) -> PreprocessResult:
    """표준 캔들 DataFrame에 프리셋을 적용해 라벨 지점 + 샘플 + 통계를 만든다.

    df는 ingestion 캐시 로드 결과 (Time 인덱스 + OHLCV/Amount). 이평선은
    ignore 규칙·필터·피처에 필요한 기간을 여기서 일괄 계산한다.
    """
    enriched = add_moving_averages(df, windows=preset.required_ma_windows)
    points, label_stats = label_points(
        enriched,
        n=preset.fractal.n,
        labeling=preset.labeling,
        filters=preset.filters,
    )
    samples, dropped_nan = build_samples(
        enriched, points, preset.sample.max_len, preset.features
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
        **label_stats,
        "confirmation_lag": confirmation_lag(preset.fractal.n),
    }
    return PreprocessResult(
        frame=enriched,
        points=points,
        samples=samples,
        feature_columns=list(preset.features),
        stats=stats,
    )
