"""M5 실시간 후보 구성과 학습 입력·체크포인트 재사용 계약."""

import hashlib
import io

import numpy as np
import pytest
import torch

from pivot.config import (
    CleaningConfig,
    FractalConfig,
    LabelingConfig,
    PreprocessPreset,
)
from pivot.dataset.build import run_preprocess
from pivot.dataset.transforms import sample_standardize
from pivot.models import build_model
from pivot.realtime.infer import (
    LiveInferenceEngine,
    build_candidate_windows,
    infer_candidates,
    preset_from_checkpoint,
)
from pivot.training.checkpoint import CheckpointError, load_verified_checkpoint

from fakes import make_candles


def preset(pairing: str = "adjacent_markers_v1") -> PreprocessPreset:
    return PreprocessPreset(
        name="live-test",
        fractal=FractalConfig(n=5),
        labeling=LabelingConfig(
            ignore_rule="none",
            sample_pairing=pairing,
        ),
        ma_windows=[5],
        features=["Open", "High", "Low", "Close"],
        cleaning=CleaningConfig(mode="off"),
    )


def checkpoint_bytes(
    feature_columns: list[str], *, dataset_snapshot: dict | None = None
) -> tuple[bytes, str]:
    config = {"model": "cnn1d_temporal_v1", "epochs": 1, "batch_size": 2}
    model = build_model(config["model"], len(feature_columns))
    buffer = io.BytesIO()
    torch.save(
        {
            "state_dict": model.state_dict(),
            "config": config,
            "feature_columns": feature_columns,
            "dataset_snapshot": dataset_snapshot
            or {"dataset": {"id": 1, "feature_columns": feature_columns}},
        },
        buffer,
    )
    data = buffer.getvalue()
    return data, hashlib.sha256(data).hexdigest()


def test_adjacent_candidate_uses_latest_retained_marker_and_current_bar():
    frame = make_candles(length=240)
    config = preset()
    result = run_preprocess(frame, config)

    candidates = build_candidate_windows(frame, config, config.features)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.pairing_rule == "adjacent_markers_v1"
    assert candidate.shared_window is True
    assert candidate.anchor_position == int(result.points.iloc[-1]["position"])
    assert candidate.end_position == len(result.frame) - 1
    np.testing.assert_array_equal(
        candidate.features,
        result.frame[config.features]
        .iloc[candidate.anchor_position : candidate.end_position + 1]
        .to_numpy(),
    )


def test_legacy_candidates_use_latest_low_and_high_anchors():
    frame = make_candles(length=240)
    config = preset("latest_opposite_v1")
    result = run_preprocess(frame, config)

    candidates = build_candidate_windows(frame, config, config.features)

    assert {candidate.target_label for candidate in candidates} == {0, 1}
    assert all(candidate.shared_window is False for candidate in candidates)
    latest = {
        kind: int(result.points[result.points["kind"] == kind].iloc[-1]["position"])
        for kind in ("low", "high")
    }
    by_label = {candidate.target_label: candidate for candidate in candidates}
    assert by_label[1].anchor_position == latest["low"]
    assert by_label[0].anchor_position == latest["high"]


def test_checkpoint_loader_verifies_digest_schema_and_feature_order():
    columns = ["Open", "High", "Low", "Close"]
    data, digest = checkpoint_bytes(columns)

    loaded = load_verified_checkpoint(data, digest)

    assert loaded.feature_columns == columns
    assert loaded.config.model == "cnn1d_temporal_v1"
    assert loaded.model.training is False
    with pytest.raises(CheckpointError, match="checksum"):
        load_verified_checkpoint(data, "0" * 64)


def test_checkpoint_preset_hydrates_missing_pairing_as_legacy():
    config = preset().model_dump(mode="json")
    config["labeling"].pop("sample_pairing")
    snapshot = {
        "dataset": {
            "id": 1,
            "feature_columns": config["features"],
            "preset_snapshot": {"schema_version": 1, "preset": config},
        }
    }
    data, digest = checkpoint_bytes(config["features"], dataset_snapshot=snapshot)

    hydrated = preset_from_checkpoint(load_verified_checkpoint(data, digest))

    assert hydrated.labeling.sample_pairing == "latest_opposite_v1"


def test_live_inference_reuses_sample_standardization():
    frame = make_candles(length=240)
    config = preset()
    candidates = build_candidate_windows(frame, config, config.features)
    data, digest = checkpoint_bytes(config.features)
    loaded = load_verified_checkpoint(data, digest)

    predictions = infer_candidates(loaded, candidates, device=torch.device("cpu"))

    assert len(predictions) == 1
    assert sum(predictions[0].probabilities) == pytest.approx(1.0)
    expected = sample_standardize(candidates[0].features)
    np.testing.assert_allclose(predictions[0].standardized_features, expected)


def test_live_engine_is_idempotent_per_deployment_and_closed_bar():
    config = preset()
    snapshot = {
        "dataset": {
            "id": 1,
            "feature_columns": config.features,
            "preset_snapshot": {
                "schema_version": 1,
                "preset": config.model_dump(mode="json"),
            },
        }
    }
    data, digest = checkpoint_bytes(config.features, dataset_snapshot=snapshot)
    engine = LiveInferenceEngine(
        load_verified_checkpoint(data, digest),
        deployment_id=7,
        device=torch.device("cpu"),
    )
    frame = make_candles(length=240)

    first = engine.infer("005930", frame)

    assert first is not None
    assert first.deployment_id == 7
    assert first.timeframe == "day"
    assert len(first.scores) == 3
    assert engine.infer("005930", frame) is None


def test_live_engine_model_forward_warmup():
    config = preset()
    snapshot = {
        "dataset": {
            "id": 1,
            "feature_columns": config.features,
            "preset_snapshot": {
                "schema_version": 1,
                "preset": config.model_dump(mode="json"),
            },
        }
    }
    data, digest = checkpoint_bytes(config.features, dataset_snapshot=snapshot)
    engine = LiveInferenceEngine(
        load_verified_checkpoint(data, digest),
        deployment_id=8,
        device=torch.device("cpu"),
    )

    engine.warmup()
