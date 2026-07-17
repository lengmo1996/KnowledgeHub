"""Fusion operations used by the tiny model."""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray


def addition(a: NDArray[np.float64], b: NDArray[np.float64]) -> NDArray[np.float64]:
    if a.shape != b.shape:
        raise ValueError("addition fusion requires identical feature shapes")
    return a + b


def concatenate(a: NDArray[np.float64], b: NDArray[np.float64]) -> NDArray[np.float64]:
    if a.shape[0] != b.shape[0]:
        raise ValueError("fusion batches must have the same length")
    return np.concatenate((a, b), axis=1)
