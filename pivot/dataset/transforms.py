"""학습과 추론이 함께 쓰는 torch 비의존 시퀀스 변환."""

from __future__ import annotations

import numpy as np


def sample_standardize(features: np.ndarray) -> np.ndarray:
    """샘플 하나를 피처별 평균 0, 표준편차 1로 변환한다.

    상수 피처는 0으로 만든다. 입력을 변경하지 않으며 float32를 반환한다.
    """
    values = np.asarray(features, dtype=np.float32)
    if values.ndim != 2 or values.shape[0] == 0:
        raise ValueError("features must be a non-empty [time, feature] array")
    if not np.isfinite(values).all():
        raise ValueError("features contain NaN or infinite values")

    mean = values.mean(axis=0, keepdims=True)
    std = values.std(axis=0, keepdims=True)
    safe_std = np.where(std > 0, std, 1.0)
    return ((values - mean) / safe_std).astype(np.float32, copy=False)
