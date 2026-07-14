"""학습 snapshot과 동일한 페어링·변환으로 실시간 후보를 추론한다."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np
import pandas as pd
import torch

from pivot.cleaning.kronos import analyze_kline_quality
from pivot.config import CleaningConfig, PreprocessPreset
from pivot.dataset.build import run_preprocess
from pivot.dataset.loader import collate_feature_sequences
from pivot.dataset.transforms import sample_standardize
from pivot.storage.presets import resolve_stored_preset
from pivot.training.checkpoint import LoadedCheckpoint


class LiveWarmupError(RuntimeError):
    pass


class LiveContractError(RuntimeError):
    pass


@dataclass(frozen=True)
class CandidateWindow:
    pairing_rule: str
    anchor_position: int
    anchor_time: pd.Timestamp
    anchor_kind: str
    end_position: int
    end_time: pd.Timestamp
    target_label: int | None
    shared_window: bool
    features: np.ndarray


@dataclass(frozen=True)
class CandidatePrediction:
    candidate: CandidateWindow
    probabilities: list[float]
    selected_class: int
    standardized_features: np.ndarray


@dataclass(frozen=True)
class LivePrediction:
    deployment_id: int
    symbol: str
    timeframe: str
    closed_time: pd.Timestamp
    scores: list[float]
    selected_class: int
    candidates: list[CandidatePrediction]


class LiveInferenceEngine:
    """활성 deployment 하나의 snapshot 계약과 멱등 추론을 소유한다."""

    def __init__(
        self,
        checkpoint: LoadedCheckpoint,
        *,
        deployment_id: int,
        device: torch.device,
    ) -> None:
        self.checkpoint = checkpoint
        self.deployment_id = deployment_id
        self.device = device
        self.preset = preset_from_checkpoint(checkpoint)
        self._seen: set[tuple[str, str, pd.Timestamp, int]] = set()
        self._seen_order: deque[tuple[str, str, pd.Timestamp, int]] = deque()

    def infer(self, symbol: str, frame: pd.DataFrame) -> LivePrediction | None:
        if frame.empty:
            raise LiveWarmupError("no closed candle history")
        closed_time = pd.Timestamp(frame.index[-1])
        key = (symbol, self.preset.timeframe.code, closed_time, self.deployment_id)
        if key in self._seen:
            return None
        candidates = build_candidate_windows(
            frame, self.preset, self.checkpoint.feature_columns
        )
        predictions = infer_candidates(
            self.checkpoint, candidates, device=self.device
        )
        scores = _combined_scores(predictions)
        self._remember(key)
        return LivePrediction(
            deployment_id=self.deployment_id,
            symbol=symbol,
            timeframe=self.preset.timeframe.code,
            closed_time=closed_time,
            scores=scores,
            selected_class=max(range(len(scores)), key=scores.__getitem__),
            candidates=predictions,
        )

    @torch.no_grad()
    def warmup(self) -> None:
        """Validate one forward pass before publishing an active deployment."""
        length = max(self.preset.fractal.n, 8)
        features = torch.zeros(
            (1, length, len(self.checkpoint.feature_columns)), dtype=torch.float32
        )
        lengths = torch.tensor([length], dtype=torch.long)
        model = self.checkpoint.model.to(self.device)
        output = model(features.to(self.device), lengths.to(self.device))
        if output.shape != (1, 3) or not bool(torch.isfinite(output).all()):
            raise LiveContractError("model warmup returned an invalid output")

    def _remember(self, key: tuple[str, str, pd.Timestamp, int]) -> None:
        self._seen.add(key)
        self._seen_order.append(key)
        if len(self._seen_order) > 10_000:
            self._seen.remove(self._seen_order.popleft())


def preset_from_checkpoint(checkpoint: LoadedCheckpoint) -> PreprocessPreset:
    try:
        dataset = checkpoint.dataset_snapshot["dataset"]
        snapshot = dataset["preset_snapshot"]
        preset = resolve_stored_preset(
            snapshot["preset"], schema_version=int(snapshot["schema_version"])
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LiveContractError(f"invalid dataset preset snapshot: {exc}") from exc
    if list(preset.features) != checkpoint.feature_columns:
        raise LiveContractError("preset features do not match checkpoint features")
    return preset.for_timeframe(preset.timeframe)


def build_candidate_windows(
    frame: pd.DataFrame,
    preset: PreprocessPreset,
    feature_columns: list[str],
) -> list[CandidateWindow]:
    if list(feature_columns) != list(preset.features):
        raise ValueError("checkpoint feature columns do not match the preset snapshot")
    active, active_preset = _active_frame(frame, preset)
    if active.empty:
        raise LiveWarmupError("current bar is outside a retained cleaning segment")
    result = run_preprocess(active, active_preset)
    if result.frame.empty or result.points.empty:
        raise LiveWarmupError("not enough confirmed fractal history")
    end = len(result.frame) - 1
    points = result.points[result.points["position"] < end]
    if points.empty:
        raise LiveWarmupError("no confirmed anchor before the current bar")

    if preset.labeling.sample_pairing == "adjacent_markers_v1":
        return [_candidate(result.frame, points.iloc[-1], end, feature_columns, None, True)]

    candidates: list[CandidateWindow] = []
    for anchor_kind, target_label in (("high", 0), ("low", 1)):
        anchors = points[points["kind"] == anchor_kind]
        if anchors.empty:
            raise LiveWarmupError(f"no confirmed {anchor_kind} anchor")
        candidates.append(
            _candidate(
                result.frame,
                anchors.iloc[-1],
                end,
                feature_columns,
                target_label,
                False,
            )
        )
    return candidates


@torch.no_grad()
def infer_candidates(
    checkpoint: LoadedCheckpoint,
    candidates: list[CandidateWindow],
    *,
    device: torch.device,
) -> list[CandidatePrediction]:
    if not candidates:
        raise LiveWarmupError("no candidate windows")
    if any(
        candidate.features.shape[1] != len(checkpoint.feature_columns)
        for candidate in candidates
    ):
        raise LiveContractError("candidate feature dimension does not match checkpoint")
    standardized = [sample_standardize(candidate.features) for candidate in candidates]
    features, lengths, _ = collate_feature_sequences(standardized)
    model = checkpoint.model.to(device)
    model.eval()
    probabilities = torch.softmax(
        model(features.to(device), lengths.to(device)), dim=1
    ).cpu()
    return [
        CandidatePrediction(
            candidate=candidate,
            probabilities=[float(value) for value in probabilities[index]],
            selected_class=int(probabilities[index].argmax().item()),
            standardized_features=standardized[index],
        )
        for index, candidate in enumerate(candidates)
    ]


def _combined_scores(predictions: list[CandidatePrediction]) -> list[float]:
    if len(predictions) == 1 and predictions[0].candidate.shared_window:
        return predictions[0].probabilities
    by_target = {
        prediction.candidate.target_label: prediction for prediction in predictions
    }
    if 0 not in by_target or 1 not in by_target:
        raise LiveContractError("legacy inference requires low and high candidates")
    return [
        by_target[0].probabilities[0],
        by_target[1].probabilities[1],
        max(prediction.probabilities[2] for prediction in predictions),
    ]


def _candidate(
    frame: pd.DataFrame,
    anchor: pd.Series,
    end: int,
    feature_columns: list[str],
    target_label: int | None,
    shared: bool,
) -> CandidateWindow:
    start = int(anchor["position"])
    values = frame[feature_columns].iloc[start : end + 1].to_numpy(dtype=np.float64)
    if not np.isfinite(values).all():
        raise LiveWarmupError("candidate features contain NaN or infinite values")
    return CandidateWindow(
        pairing_rule="adjacent_markers_v1" if shared else "latest_opposite_v1",
        anchor_position=start,
        anchor_time=pd.Timestamp(frame.index[start]),
        anchor_kind=str(anchor["kind"]),
        end_position=end,
        end_time=pd.Timestamp(frame.index[end]),
        target_label=target_label,
        shared_window=shared,
        features=values,
    )


def _active_frame(
    frame: pd.DataFrame, preset: PreprocessPreset
) -> tuple[pd.DataFrame, PreprocessPreset]:
    if preset.cleaning.mode != "filter":
        return frame, preset
    required_bars = max([preset.fractal.n, *preset.required_ma_windows], default=1)
    analysis = analyze_kline_quality(
        frame,
        timeframe=preset.timeframe,
        config=preset.cleaning,
        required_bars=required_bars,
    )
    segment = next(
        (item for item in analysis.segments if item.end == len(frame) - 1),
        None,
    )
    if segment is None:
        return frame.iloc[0:0], preset
    active = frame.iloc[segment.start : segment.end + 1].copy()
    return active, preset.model_copy(update={"cleaning": CleaningConfig(mode="off")})
