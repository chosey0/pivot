import pytest

from pivot.training.metrics import classification_metrics


def test_metrics_keep_all_three_classes_when_one_is_unpredicted():
    result = classification_metrics([0, 1, 2, 2], [0, 0, 0, 2])

    assert result["accuracy"] == 0.5
    assert result["confusion_matrix"] == [[1, 0, 0], [1, 0, 0], [1, 0, 1]]
    assert result["per_class_metrics"]["1"] == {
        "precision": 0.0,
        "recall": 0.0,
        "f1": 0.0,
        "support": 1,
    }
    assert result["macro_f1"] == pytest.approx((0.5 + 0.0 + 2 / 3) / 3)


def test_metrics_reject_empty_input():
    with pytest.raises(ValueError, match="at least one"):
        classification_metrics([], [])
