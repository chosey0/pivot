from pivot.dataset.build import Sample
from pivot.dataset.overlap import analyze_overlap_clusters


def sample(start: int, end: int, *, kind: str = "low", label: int = 0) -> Sample:
    return Sample(
        start_position=start,
        end_position=end,
        kind=kind,
        label=label,
        price=100.0,
        length=end - start + 1,
    )


def test_contained_consecutive_windows_form_one_overlap_cluster():
    stats = analyze_overlap_clusters(
        [sample(10, 15), sample(10, 16), sample(10, 17), sample(17, 25, kind="high", label=1)],
        max_end_gap=9,
    )

    assert stats == {
        "threshold": 0.9,
        "max_end_gap": 9,
        "clusters": 1,
        "clustered_samples": 3,
        "redundant_samples": 2,
        "max_cluster_size": 3,
    }


def test_opposite_kind_breaks_overlap_cluster():
    stats = analyze_overlap_clusters(
        [sample(10, 15), sample(10, 16, kind="high", label=1), sample(10, 17)],
        max_end_gap=9,
    )
    assert stats["clusters"] == 0


def test_shifted_windows_with_ninety_percent_overlap_form_cluster():
    stats = analyze_overlap_clusters(
        [sample(10, 109), sample(20, 119)],
        max_end_gap=10,
    )
    assert stats["clusters"] == 1
    assert stats["redundant_samples"] == 1


def test_stored_metadata_uses_same_start_time_contract():
    stats = analyze_overlap_clusters(
        [
            {"start_time": "1997-07-10", "end_time": "1997-07-16", "length": 6, "kind": "low", "label": 0},
            {"start_time": "1997-07-10", "end_time": "1997-07-18", "length": 7, "kind": "low", "label": 0},
            {"start_time": "1997-07-10", "end_time": "1997-07-19", "length": 8, "kind": "low", "label": 0},
        ],
        max_end_gap=9,
    )
    assert stats["clusters"] == 1
    assert stats["redundant_samples"] == 2
