"""저장소와 무관한 CNN 학습 루프."""

from __future__ import annotations

import copy
import random
from collections import Counter
from collections.abc import Callable

import numpy as np
import torch
from torch.utils.data import DataLoader, WeightedRandomSampler

from pivot.config import TrainingConfig
from pivot.dataset.loader import collate_samples
from pivot.training.evaluate import evaluate_model
from pivot.training.metrics import classification_metrics


class TrainingCancelled(Exception):
    pass


def select_device() -> torch.device:
    if torch.backends.mps.is_available():
        return torch.device("mps")
    if torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")


def seed_everything(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)


def make_loader(dataset, config: TrainingConfig, *, training: bool) -> DataLoader:
    sampler = None
    shuffle = training
    if training and config.sampler == "weighted":
        labels = dataset.labels()
        counts = Counter(labels)
        weights = torch.tensor([1.0 / counts[label] for label in labels])
        generator = torch.Generator().manual_seed(config.seed)
        sampler = WeightedRandomSampler(
            weights, num_samples=len(weights), replacement=True, generator=generator
        )
        shuffle = False
    return DataLoader(
        dataset,
        batch_size=config.batch_size,
        shuffle=shuffle,
        sampler=sampler,
        collate_fn=collate_samples,
        generator=torch.Generator().manual_seed(config.seed),
    )


def train_model(
    model,
    train_dataset,
    validation_dataset,
    config: TrainingConfig,
    *,
    device: torch.device,
    cancelled: Callable[[], bool] = lambda: False,
    on_epoch: Callable[[int, dict], None] = lambda _epoch, _metrics: None,
) -> dict:
    seed_everything(config.seed)
    model.to(device)
    train_loader = make_loader(train_dataset, config, training=True)
    validation_loader = make_loader(validation_dataset, config, training=False)
    criterion = torch.nn.CrossEntropyLoss()
    optimizer = torch.optim.Adam(model.parameters(), lr=config.learning_rate)
    best_epoch = -1
    best_metric = -1.0
    best_state = None

    for epoch in range(config.epochs):
        if cancelled():
            raise TrainingCancelled
        model.train()
        total_loss = 0.0
        actual: list[int] = []
        predicted: list[int] = []
        for batch in train_loader:
            if cancelled():
                raise TrainingCancelled
            features = batch["features"].to(device)
            labels = batch["labels"].to(device)
            lengths = batch["lengths"].to(device)
            optimizer.zero_grad(set_to_none=True)
            logits = model(features, lengths)
            loss = criterion(logits, labels)
            loss.backward()
            optimizer.step()
            total_loss += float(loss.item()) * len(labels)
            actual.extend(labels.cpu().tolist())
            predicted.extend(logits.argmax(dim=1).detach().cpu().tolist())

        train_metrics = classification_metrics(actual, predicted)
        validation = evaluate_model(model, validation_loader, device)["metrics"]
        metrics = {
            "train_loss": total_loss / len(train_dataset),
            "train_accuracy": train_metrics["accuracy"],
            "train_macro_f1": train_metrics["macro_f1"],
            "validation_loss": validation["loss"],
            "validation_accuracy": validation["accuracy"],
            "validation_macro_f1": validation["macro_f1"],
            "learning_rate": optimizer.param_groups[0]["lr"],
        }
        on_epoch(epoch, metrics)
        if metrics["validation_macro_f1"] > best_metric:
            best_epoch = epoch
            best_metric = metrics["validation_macro_f1"]
            best_state = copy.deepcopy(model.state_dict())

    if best_state is None:
        raise RuntimeError("training produced no checkpoint")
    model.load_state_dict(best_state)
    return {
        "model": model,
        "best_state": best_state,
        "best_epoch": best_epoch,
        "best_metric_name": "val_macro_f1",
        "best_metric_value": best_metric,
    }
