"""Small deterministic synthetic classification data."""

from __future__ import annotations

from typing import Any

import numpy as np
from numpy.typing import NDArray


def generate(config: dict[str, Any]) -> dict[str, tuple[NDArray[np.float64], ...]]:
    rng = np.random.default_rng(int(config["seed"]))
    count = int(config["samples"])
    dimension = int(config["input_dim"])
    latent = rng.normal(size=(count, dimension))
    view_a = latent + rng.normal(scale=0.25, size=latent.shape)
    rotation = np.roll(latent, shift=1, axis=1)
    view_b = rotation + rng.normal(scale=0.25, size=latent.shape)
    signal = latent[:, 0] + 0.8 * latent[:, 1] - 0.5 * latent[:, 2] + 0.2 * latent[:, 3]
    labels = (signal > 0).astype(np.float64)
    order = rng.permutation(count)
    train_end = int(count * 0.6)
    validation_end = int(count * 0.8)

    def split(indices: NDArray[np.int64]) -> tuple[NDArray[np.float64], ...]:
        return view_a[indices], view_b[indices], labels[indices]

    return {
        "train": split(order[:train_end]),
        "validation": split(order[train_end:validation_end]),
        "test": split(order[validation_end:]),
    }
