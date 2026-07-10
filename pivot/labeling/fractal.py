"""윌리엄스 프랙탈 라벨링 (docs/01 §3 재구현).

크기 n의 center rolling window에서 중심 봉의 High가 창 내 최댓값이면 프랙탈 고점,
Low가 최솟값이면 프랙탈 저점이다. 창 정렬은 구 파이프라인(pandas
`rolling(n, center=True)`)과 동일하게 과거 `n//2`봉 + 중심 + 미래 `(n-1)//2`봉이다
(짝수 n이면 pandas는 남는 한 봉을 과거 쪽에 붙인다 — 실측 고정, tests 참고).

lag 처리: 프랙탈은 미래 `(n-1)//2`봉이 지나야 확정되는 후행 지표다. 시리즈 마지막
`(n-1)//2`봉은 확인 창이 부족하므로 **절대 라벨하지 않는다** (미확정 구간).
시리즈 시작 `n//2`봉도 과거 창이 부족해 라벨하지 않는다.
창 내 동일 극값(tie)은 구 방식대로 모두 마킹한다.

라벨 규약: 0 = 프랙탈 저점, 1 = 프랙탈 고점, 2 = 무시 (기본 규칙: 라벨 봉에서
MA20 < MA120 역배열). 단건 preview와 일괄 batch가 모두 이 모듈을 호출한다.
"""

import pandas as pd

from pivot.config import FilterConfig, LabelingConfig

LABEL_LOW = 0
LABEL_HIGH = 1
LABEL_IGNORE = 2


def confirmation_lag(n: int) -> int:
    """라벨 확정에 필요한 미래 봉 수 (pandas center rolling 정렬 기준)."""
    return (n - 1) // 2


def calc_fractal(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """`fractal_high`/`fractal_low` 컬럼(확정 시 해당 가격, 아니면 NaN)을 추가한 사본.

    창이 시리즈 경계를 벗어나는 양 끝 구간은 NaN으로 남는다 — 특히 마지막
    `n//2`봉은 미래 확인 봉이 부족한 미확정 구간이다.
    """
    if n < 3:
        raise ValueError("fractal n must be >= 3")
    future = confirmation_lag(n)

    out = df.copy()
    # rolling(n).max()는 [i-n+1, i] 창이므로 shift(-future)로 중심 정렬:
    # 위치 i의 창 = [i - n//2, i + (n-1)//2] — pandas center rolling과 동일
    window_high = out["High"].rolling(n, min_periods=n).max().shift(-future)
    window_low = out["Low"].rolling(n, min_periods=n).min().shift(-future)
    out["fractal_high"] = out["High"].where(out["High"] == window_high)
    out["fractal_low"] = out["Low"].where(out["Low"] == window_low)
    return out


def _passes_filters(df: pd.DataFrame, filters: FilterConfig) -> pd.Series:
    """각 봉이 필터를 통과하는지 여부. MA가 NaN인 구간은 통과하지 못한다."""
    mask = pd.Series(True, index=df.index)
    if filters.ma_alignment == "20>120":
        mask &= df["20"] > df["120"]
    elif filters.ma_alignment == "5>20>120":
        mask &= (df["5"] > df["20"]) & (df["20"] > df["120"])
    if filters.min_amount is not None:
        mask &= df["Amount"] >= filters.min_amount
    return mask


def label_points(
    df: pd.DataFrame,
    n: int,
    labeling: LabelingConfig | None = None,
    filters: FilterConfig | None = None,
) -> tuple[pd.DataFrame, dict]:
    """프랙탈 지점에 라벨을 부여한다.

    반환: (points, stats)
    - points: 라벨 지점별 한 행 — `position`(원본 iloc 위치), `kind`(low|high),
      `price`(프랙탈 가격), `label`(0/1/2). Time 인덱스 유지, 시간 오름차순.
      같은 봉이 고점·저점 동시 확정이면 두 행이 된다 (구 방식의 low/high 루프 계승).
    - stats: dropped_filters / dropped_ignore(cls2_drop일 때) 카운트.
    """
    labeling = labeling or LabelingConfig()
    filters = filters or FilterConfig()
    marked = calc_fractal(df, n)
    passes = _passes_filters(marked, filters)

    rows: list[dict] = []
    dropped_filters = 0
    dropped_ignore = 0
    for kind, column, base_label in (
        ("low", "fractal_low", LABEL_LOW),
        ("high", "fractal_high", LABEL_HIGH),
    ):
        hits = marked.index[marked[column].notna()]
        for time in hits:
            if not passes.loc[time]:
                dropped_filters += 1
                continue
            label = base_label
            if labeling.ignore_rule == "ma20<ma120":
                ma20, ma120 = marked.at[time, "20"], marked.at[time, "120"]
                if pd.notna(ma20) and pd.notna(ma120) and ma20 < ma120:
                    label = LABEL_IGNORE
            if label == LABEL_IGNORE and labeling.mode == "cls2_drop":
                dropped_ignore += 1
                continue
            rows.append(
                {
                    "time": time,
                    "position": marked.index.get_loc(time),
                    "kind": kind,
                    "price": marked.at[time, column],
                    "label": label,
                }
            )

    points = pd.DataFrame(
        rows, columns=["time", "position", "kind", "price", "label"]
    ).set_index("time")
    points = points.sort_values(["position", "kind"])
    stats = {"dropped_filters": dropped_filters, "dropped_ignore": dropped_ignore}
    return points, stats
