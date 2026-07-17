"""Structured fixture metrics."""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray


def binary_metrics(probabilities: NDArray[np.float64], labels: NDArray[np.float64]) -> dict[str, float]:
    epsilon = 1e-8
    loss = -np.mean(
        labels * np.log(probabilities + epsilon)
        + (1 - labels) * np.log(1 - probabilities + epsilon)
    )
    accuracy = np.mean((probabilities >= 0.5) == labels)
    return {"accuracy": float(accuracy), "loss": float(loss)}
