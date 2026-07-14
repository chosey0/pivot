"""모델 평가와 차트 매핑용 sample-keyed 예측."""

from __future__ import annotations

import torch

from pivot.training.metrics import classification_metrics


@torch.no_grad()
def evaluate_model(model, loader, device: torch.device) -> dict:
    model.eval()
    actual: list[int] = []
    predicted: list[int] = []
    points: list[dict] = []
    total_loss = 0.0
    total = 0
    criterion = torch.nn.CrossEntropyLoss(reduction="sum")
    for batch in loader:
        features = batch["features"].to(device)
        labels = batch["labels"].to(device)
        lengths = batch["lengths"].to(device)
        logits = model(features, lengths)
        probabilities = torch.softmax(logits, dim=1)
        guesses = probabilities.argmax(dim=1)
        total_loss += float(criterion(logits, labels).item())
        total += len(labels)
        actual.extend(labels.cpu().tolist())
        predicted.extend(guesses.cpu().tolist())
        for index in range(len(labels)):
            expected = int(labels[index].item())
            guess = int(guesses[index].item())
            points.append(
                {
                    "symbol": batch["symbols"][index],
                    "sample_index": batch["sample_indices"][index],
                    "time": batch["end_times"][index],
                    "timeframe": batch["timeframes"][index],
                    "source_key": batch["source_keys"][index],
                    "actual_label": expected,
                    "predicted_label": guess,
                    "probabilities": [
                        float(value) for value in probabilities[index].cpu()
                    ],
                    "correct": expected == guess,
                }
            )
    metrics = classification_metrics(actual, predicted)
    metrics["loss"] = total_loss / total
    return {"metrics": metrics, "points": points}
