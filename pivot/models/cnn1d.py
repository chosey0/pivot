"""가변 길이 캔들 시퀀스를 위한 CNN1D 베이스라인."""

from __future__ import annotations

import torch
from torch import nn
from torch.nn import functional as F


def _validate(features: torch.Tensor, lengths: torch.Tensor) -> None:
    if features.ndim != 3:
        raise ValueError("features must have shape [batch, time, feature]")
    if lengths.ndim != 1 or len(lengths) != len(features):
        raise ValueError("lengths must have shape [batch]")
    if bool(((lengths <= 0) | (lengths > features.shape[1])).any()):
        raise ValueError("lengths contain an invalid sequence length")


def _mask(lengths: torch.Tensor, time_steps: int) -> torch.Tensor:
    positions = torch.arange(time_steps, device=lengths.device).unsqueeze(0)
    return (positions < lengths.unsqueeze(1)).unsqueeze(1)


def _adaptive_avg_8(value: torch.Tensor, length: int) -> torch.Tensor:
    """MPS가 지원하지 않는 비정수 AdaptiveAvgPool1d(8)의 동일 bin 계산."""
    bins = []
    for index in range(8):
        start = index * length // 8
        end = ((index + 1) * length + 7) // 8
        bins.append(value[:, :, start:end].mean(dim=2, keepdim=True))
    return torch.cat(bins, dim=2)


class LegacyCNN1D(nn.Module):
    """구 Fractal의 1x1 conv + adaptive pooling 구조를 재현한 기준 모델."""

    def __init__(self, input_size: int, output_size: int = 3) -> None:
        super().__init__()
        self.input_size = input_size
        self.conv1 = nn.Conv1d(input_size, input_size * 2, kernel_size=1)
        self.conv2 = nn.Conv1d(input_size * 2, input_size * 4, kernel_size=1)
        self.conv3 = nn.Conv1d(input_size * 4, input_size * 8, kernel_size=1)
        self.fc1 = nn.Linear(input_size * 8 * 8, input_size * 8 * 4)
        self.fc2 = nn.Linear(input_size * 8 * 4, output_size)

    def forward(self, features: torch.Tensor, lengths: torch.Tensor) -> torch.Tensor:
        _validate(features, lengths)
        value = F.relu(self.conv1(features.transpose(1, 2)))
        # 첫 pool만 가변 길이에 의존한다. 각 유효 prefix를 8칸으로 만든 뒤
        # 나머지 legacy stack은 고정 길이 batch로 처리한다.
        value = torch.cat(
            [
                _adaptive_avg_8(row, int(length.item()))
                for row, length in zip(value.split(1), lengths, strict=True)
            ]
        )
        value = F.relu(self.conv2(value))
        value = F.relu(self.conv3(value))
        return self.fc2(F.relu(self.fc1(value.flatten(1))))


class TemporalCNN1D(nn.Module):
    """시간축 이웃 패턴을 보는 kernel 기반 비교 모델."""

    def __init__(self, input_size: int, output_size: int = 3) -> None:
        super().__init__()
        width = max(input_size * 4, 16)
        self.conv1 = nn.Conv1d(input_size, width, kernel_size=5, padding=2)
        self.conv2 = nn.Conv1d(width, width * 2, kernel_size=3, padding=1)
        self.conv3 = nn.Conv1d(width * 2, width * 2, kernel_size=3, padding=1)
        self.classifier = nn.Linear(width * 2, output_size)

    def forward(self, features: torch.Tensor, lengths: torch.Tensor) -> torch.Tensor:
        _validate(features, lengths)
        valid = _mask(lengths, features.shape[1]).to(features.dtype)
        value = F.relu(self.conv1(features.transpose(1, 2))) * valid
        value = F.relu(self.conv2(value)) * valid
        value = F.relu(self.conv3(value)) * valid
        pooled = value.sum(dim=2) / lengths.to(value.dtype).unsqueeze(1)
        return self.classifier(pooled)


def build_model(name: str, input_size: int, output_size: int = 3) -> nn.Module:
    models = {
        "cnn1d_legacy_v1": LegacyCNN1D,
        "cnn1d_temporal_v1": TemporalCNN1D,
    }
    try:
        model = models[name]
    except KeyError as exc:
        raise ValueError(f"unsupported model: {name}") from exc
    return model(input_size, output_size)
