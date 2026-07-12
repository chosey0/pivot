"""고정 3클래스 학습·평가 지표."""

from __future__ import annotations

import numpy as np

LABELS = (0, 1, 2)


def classification_metrics(actual, predicted) -> dict:
    truth = np.asarray(actual, dtype=np.int64)
    guesses = np.asarray(predicted, dtype=np.int64)
    if truth.shape != guesses.shape or truth.ndim != 1:
        raise ValueError("actual and predicted must be same-length 1D arrays")
    if len(truth) == 0:
        raise ValueError("metrics require at least one sample")

    confusion = np.zeros((3, 3), dtype=np.int64)
    for expected, guessed in zip(truth, guesses, strict=True):
        if expected not in LABELS or guessed not in LABELS:
            raise ValueError("labels must be 0, 1, or 2")
        confusion[expected, guessed] += 1

    per_class: dict[str, dict] = {}
    f1_values = []
    for label in LABELS:
        true_positive = int(confusion[label, label])
        predicted_count = int(confusion[:, label].sum())
        support = int(confusion[label, :].sum())
        precision = true_positive / predicted_count if predicted_count else 0.0
        recall = true_positive / support if support else 0.0
        f1 = (
            2 * precision * recall / (precision + recall) if precision + recall else 0.0
        )
        per_class[str(label)] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "support": support,
        }
        f1_values.append(f1)

    return {
        "accuracy": float((truth == guesses).mean()),
        "macro_f1": float(np.mean(f1_values)),
        "confusion_matrix": confusion.tolist(),
        "per_class_metrics": per_class,
    }
