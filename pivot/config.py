"""프리셋/타임프레임 설정 스키마. docs/03 §2, docs/04 §2 참고."""

from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

MINUTE_UNITS = (1, 3, 5, 10, 15, 30, 45, 60)
TICK_UNITS = (1, 3, 5, 10, 30)
DEFAULT_MA_WINDOWS = (5, 20, 60, 120)
BASE_FEATURES = ("Open", "High", "Low", "Close")
OPTIONAL_RAW_FEATURES = ("Volume", "Amount")


class Timeframe(BaseModel):
    """봉 종류. 코드 표기는 day | min{N} | tick{N} (docs/03 §2)."""

    type: Literal["day", "minute", "tick"] = "day"
    unit: int = 1

    @model_validator(mode="after")
    def _check_unit(self) -> Self:
        if self.type == "day" and self.unit != 1:
            raise ValueError("day timeframe has no unit")
        if self.type == "minute" and self.unit not in MINUTE_UNITS:
            raise ValueError(f"minute unit must be one of {MINUTE_UNITS}")
        if self.type == "tick" and self.unit not in TICK_UNITS:
            raise ValueError(f"tick unit must be one of {TICK_UNITS}")
        return self

    @property
    def code(self) -> str:
        if self.type == "day":
            return "day"
        prefix = "min" if self.type == "minute" else "tick"
        return f"{prefix}{self.unit}"

    @classmethod
    def from_code(cls, code: str) -> "Timeframe":
        if code == "day":
            return cls(type="day")
        for prefix, type_ in (("min", "minute"), ("tick", "tick")):
            if code.startswith(prefix) and code[len(prefix):].isdigit():
                return cls(type=type_, unit=int(code[len(prefix):]))
        raise ValueError(f"invalid timeframe code: {code!r}")


class MovingAverageIndicator(BaseModel):
    """이동평균선 표시/학습 피처 설정."""

    window: int
    color: str = "#64748b"
    line_width: int = 1
    chart: bool = True
    feature: bool = False

    @model_validator(mode="after")
    def _check_values(self) -> Self:
        if self.window <= 0:
            raise ValueError("moving average windows must be positive")
        if self.line_width < 1:
            raise ValueError("line_width must be positive")
        return self


class VolumeIndicator(BaseModel):
    """거래량 표시/학습 피처 설정."""

    chart: bool = True
    feature: bool = False


class ChartIndicators(BaseModel):
    """차트에 표시할 보조지표. lightweight-charts series 구성에 대응한다."""

    preset: str = "기본 MA 5/20/60/120"
    moving_averages: list[MovingAverageIndicator] = Field(
        default_factory=lambda: [
            MovingAverageIndicator(
                window=5, color="#009c62", line_width=1, chart=True, feature=False
            ),
            MovingAverageIndicator(
                window=20, color="#e31b35", line_width=1, chart=True, feature=True
            ),
            MovingAverageIndicator(
                window=60, color="#ff8a00", line_width=1, chart=True, feature=False
            ),
            MovingAverageIndicator(
                window=120, color="#8a26b2", line_width=1, chart=True, feature=True
            ),
        ]
    )
    volume: VolumeIndicator = Field(default_factory=VolumeIndicator)

    @property
    def ma_windows(self) -> list[int]:
        return [indicator.window for indicator in self.moving_averages]

    @property
    def feature_columns(self) -> list[str]:
        return [
            *(["Volume"] if self.volume.feature else []),
            *[
                str(indicator.window)
                for indicator in self.moving_averages
                if indicator.feature
            ],
        ]

    @model_validator(mode="after")
    def _check_windows(self) -> Self:
        if duplicate := {
            window
            for window in self.ma_windows
            if self.ma_windows.count(window) > 1
        }:
            raise ValueError(f"duplicate moving average windows: {sorted(duplicate)}")
        return self


class FractalConfig(BaseModel):
    """윌리엄스 프랙탈 center rolling window 설정.

    창 크기 n의 프랙탈은 미래 `(n-1) // 2`봉이 지나야 확정되는 후행 지표다
    (pandas center rolling 정렬, pivot/labeling/fractal.py 참고).
    """

    n: int = 20

    @model_validator(mode="after")
    def _check_n(self) -> Self:
        if self.n < 3:
            raise ValueError("fractal n must be >= 3")
        return self

    @property
    def confirmation_lag(self) -> int:
        return (self.n - 1) // 2


class SampleConfig(BaseModel):
    """시퀀스 샘플 설정. max_len = 라벨 봉을 끝으로 하는 입력 윈도우 길이 상한."""

    max_len: int = 20

    @model_validator(mode="after")
    def _check_max_len(self) -> Self:
        if self.max_len < 1:
            raise ValueError("sample max_len must be >= 1")
        return self


class LabelingConfig(BaseModel):
    """라벨 규약: 0=저점, 1=고점, 2=무시 (백로그 B2 모드화).

    - cls3: 무시 규칙에 걸린 고점/저점을 label 2로 덮어씀 (구 방식)
    - cls2_drop: 무시 규칙에 걸린 샘플을 제외하고 0/1만 유지
    """

    mode: Literal["cls3", "cls2_drop"] = "cls3"
    ignore_rule: Literal["ma20<ma120", "none"] = "ma20<ma120"


class FilterConfig(BaseModel):
    """라벨링 단계 필터 (백로그 B5). 걸린 프랙탈 지점은 샘플에서 제외한다."""

    ma_alignment: Literal["20>120", "5>20>120"] | None = None
    min_amount: int | None = None

    @model_validator(mode="after")
    def _check_min_amount(self) -> Self:
        if self.min_amount is not None and self.min_amount < 0:
            raise ValueError("min_amount must be >= 0")
        return self


class PreprocessPreset(BaseModel):
    """전처리 프리셋.

    `features`는 학습 데이터에 들어갈 컬럼 목록이다. 차트에 보조지표를 표시하더라도
    여기에서 제외하면 데이터셋 피처로 사용하지 않는다.
    name은 저장된 프리셋(M3 CRUD)에서만 필수이고, Lab preview는 이름 없이 쓴다.
    """

    name: str = ""
    timeframe: Timeframe = Field(default_factory=Timeframe)
    fractal: FractalConfig = Field(default_factory=FractalConfig)
    ma_windows: list[int] = Field(default_factory=lambda: list(DEFAULT_MA_WINDOWS))
    chart_indicators: ChartIndicators = Field(default_factory=ChartIndicators)
    features: list[str] = Field(
        default_factory=lambda: [*BASE_FEATURES, "20", "120"]
    )
    sample: SampleConfig = Field(default_factory=SampleConfig)
    labeling: LabelingConfig = Field(default_factory=LabelingConfig)
    filters: FilterConfig = Field(default_factory=FilterConfig)

    @property
    def required_ma_windows(self) -> list[int]:
        """전처리 계산에 필요한 이평 기간 전체 (ignore 규칙·필터·피처 포함)."""
        needed = set(self.ma_windows)
        if self.labeling.ignore_rule == "ma20<ma120":
            needed |= {20, 120}
        if self.filters.ma_alignment == "20>120":
            needed |= {20, 120}
        elif self.filters.ma_alignment == "5>20>120":
            needed |= {5, 20, 120}
        for feature in self.features:
            if feature.isdigit():
                needed.add(int(feature))
        return sorted(needed)

    @model_validator(mode="after")
    def _check_features(self) -> Self:
        if any(window <= 0 for window in self.ma_windows):
            raise ValueError("moving average windows must be positive")
        ma_columns = {
            str(window)
            for window in [*self.ma_windows, *self.chart_indicators.ma_windows]
        }
        allowed = {*BASE_FEATURES, *OPTIONAL_RAW_FEATURES, *ma_columns}
        unknown = [feature for feature in self.features if feature not in allowed]
        if unknown:
            raise ValueError(f"unknown feature columns: {unknown}")
        return self
