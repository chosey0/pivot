import pytest
import torch

from pivot.models import build_model
from pivot.models.cnn1d import _adaptive_avg_8


@pytest.mark.parametrize("name", ["cnn1d_legacy_v1", "cnn1d_temporal_v1"])
def test_model_output_is_invariant_to_batch_padding(name):
    torch.manual_seed(7)
    model = build_model(name, input_size=4).eval()
    sample = torch.randn(1, 6, 4)
    single = model(sample, torch.tensor([6]))[0]

    padded = torch.zeros(2, 11, 4)
    padded[0, :6] = sample[0]
    padded[1] = torch.randn(11, 4)
    batched = model(padded, torch.tensor([6, 11]))[0]

    torch.testing.assert_close(single, batched)


def test_build_model_rejects_unknown_name():
    with pytest.raises(ValueError, match="unsupported model"):
        build_model("not-a-model", 4)


@pytest.mark.parametrize("length", [3, 7, 9, 17])
def test_portable_adaptive_pool_matches_pytorch(length):
    value = torch.randn(1, 4, length)
    expected = torch.nn.functional.adaptive_avg_pool1d(value, 8)
    torch.testing.assert_close(_adaptive_avg_8(value, length), expected)
