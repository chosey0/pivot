"""학습 평가와 실시간 추론이 공유하는 검증 checkpoint 로더."""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

import torch

from pivot.config import TrainingConfig
from pivot.models import build_model


class CheckpointError(RuntimeError):
    pass


@dataclass(frozen=True)
class LoadedCheckpoint:
    model: torch.nn.Module
    config: TrainingConfig
    feature_columns: list[str]
    dataset_snapshot: dict


def load_verified_checkpoint(
    data: bytes,
    expected_sha256: str,
    *,
    expected_config: dict | None = None,
) -> LoadedCheckpoint:
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise CheckpointError("checkpoint checksum mismatch")
    try:
        checkpoint = torch.load(io.BytesIO(data), map_location="cpu", weights_only=True)
    except Exception as exc:
        raise CheckpointError(f"cannot load checkpoint: {exc}") from exc
    required = {"state_dict", "config", "feature_columns", "dataset_snapshot"}
    if not isinstance(checkpoint, dict) or not required.issubset(checkpoint):
        raise CheckpointError("checkpoint has an invalid schema")
    try:
        config = TrainingConfig.model_validate(checkpoint["config"])
        if expected_config is not None and config != TrainingConfig.model_validate(
            expected_config
        ):
            raise ValueError("checkpoint config does not match the run")
        feature_columns = [str(column) for column in checkpoint["feature_columns"]]
        if not feature_columns or len(set(feature_columns)) != len(feature_columns):
            raise ValueError("feature columns must be non-empty and unique")
        snapshot = dict(checkpoint["dataset_snapshot"])
        snapshot_columns = (snapshot.get("dataset") or {}).get("feature_columns")
        if snapshot_columns is not None and list(snapshot_columns) != feature_columns:
            raise ValueError("checkpoint features do not match the dataset snapshot")
        model = build_model(config.model, len(feature_columns))
        model.load_state_dict(checkpoint["state_dict"])
    except Exception as exc:
        raise CheckpointError(f"checkpoint contract mismatch: {exc}") from exc
    model.eval()
    return LoadedCheckpoint(
        model=model,
        config=config,
        feature_columns=feature_columns,
        dataset_snapshot=snapshot,
    )
