import numpy as np
import pytest
import torch
from torch.utils.data import Dataset

from pivot.config import TrainingConfig
from pivot.dataset.loader import TrainingSample
from pivot.models import build_model
from pivot.training.evaluate import evaluate_model
from pivot.training.train import TrainingCancelled, make_loader, train_model


class TinyDataset(Dataset):
    def __init__(self, count: int = 12):
        self.items = [
            TrainingSample(
                features=np.asarray(
                    [[float(i + j), float(label - j)] for j in range(3 + i % 3)],
                    dtype=np.float32,
                ),
                label=label,
                symbol="AAA",
                sample_index=i,
                end_time=f"2026-01-{i + 1:02d}T00:00:00",
            )
            for i in range(count)
            for label in [i % 3]
        ]

    def __len__(self):
        return len(self.items)

    def __getitem__(self, index):
        return self.items[index]

    def labels(self):
        return [item.label for item in self.items]


def test_training_records_epochs_and_restores_best_model():
    config = TrainingConfig(epochs=2, batch_size=4, sampler="weighted", seed=3)
    dataset = TinyDataset()
    seen = []
    result = train_model(
        build_model(config.model, 2),
        dataset,
        dataset,
        config,
        device=torch.device("cpu"),
        on_epoch=lambda epoch, metrics: seen.append((epoch, metrics)),
    )

    assert [epoch for epoch, _ in seen] == [0, 1]
    assert result["best_epoch"] in (0, 1)
    assert 0 <= result["best_metric_value"] <= 1
    evaluated = evaluate_model(
        result["model"],
        make_loader(dataset, config, training=False),
        torch.device("cpu"),
    )
    assert len(evaluated["points"]) == len(dataset)
    assert evaluated["points"][0]["time"] == "2026-01-01T00:00:00"


def test_training_honors_cooperative_cancel_before_first_epoch():
    config = TrainingConfig(epochs=1, batch_size=4)
    with pytest.raises(TrainingCancelled):
        train_model(
            build_model(config.model, 2),
            TinyDataset(),
            TinyDataset(),
            config,
            device=torch.device("cpu"),
            cancelled=lambda: True,
        )
