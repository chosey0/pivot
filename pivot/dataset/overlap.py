"""서로 거의 같은 시퀀스 샘플의 연속 cluster 통계."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

DEFAULT_OVERLAP_THRESHOLD = 0.9


def _value(sample: Any, name: str) -> Any:
    return sample.get(name) if isinstance(sample, Mapping) else getattr(sample, name, None)


def _near_duplicate(
    previous: Any,
    current: Any,
    *,
    threshold: float,
    max_end_gap: int,
) -> bool:
    if (
        _value(previous, "kind") != _value(current, "kind")
        or _value(previous, "label") != _value(current, "label")
    ):
        return False

    previous_start = _value(previous, "start_position")
    current_start = _value(current, "start_position")
    previous_end = _value(previous, "end_position")
    current_end = _value(current, "end_position")
    if None not in (previous_start, current_start, previous_end, current_end):
        end_gap = int(current_end) - int(previous_end)
        if not 0 <= end_gap <= max_end_gap:
            return False
        intersection = max(
            0,
            min(int(previous_end), int(current_end))
            - max(int(previous_start), int(current_start))
            + 1,
        )
        shorter = min(int(_value(previous, "length")), int(_value(current, "length")))
        return shorter > 0 and intersection / shorter >= threshold

    # 저장된 shard 메타에는 iloc 위치가 없다. 시작 시각이 같으면 짧은 윈도우가 긴
    # 윈도우에 완전히 포함되므로 길이 차이를 종료점 거리로 사용한다.
    if _value(previous, "start_time") != _value(current, "start_time"):
        return False
    length_gap = abs(int(_value(current, "length")) - int(_value(previous, "length")))
    return length_gap <= max_end_gap


def analyze_overlap_clusters(
    samples: Sequence[Any],
    *,
    max_end_gap: int,
    threshold: float = DEFAULT_OVERLAP_THRESHOLD,
) -> dict:
    """시간순으로 인접한 near-duplicate 시퀀스를 cluster로 집계한다."""
    cluster_sizes: list[int] = []
    current_size = 1
    for previous, current in zip(samples, samples[1:]):
        if _near_duplicate(
            previous,
            current,
            threshold=threshold,
            max_end_gap=max_end_gap,
        ):
            current_size += 1
            continue
        if current_size > 1:
            cluster_sizes.append(current_size)
        current_size = 1
    if current_size > 1:
        cluster_sizes.append(current_size)

    clustered = sum(cluster_sizes)
    return {
        "threshold": threshold,
        "max_end_gap": max_end_gap,
        "clusters": len(cluster_sizes),
        "clustered_samples": clustered,
        "redundant_samples": clustered - len(cluster_sizes),
        "max_cluster_size": max(cluster_sizes, default=0),
    }
