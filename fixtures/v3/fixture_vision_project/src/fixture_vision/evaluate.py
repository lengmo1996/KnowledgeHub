"""Evaluation helper."""

from __future__ import annotations

from typing import Any

from fixture_vision.metrics import binary_metrics
from fixture_vision.model import FusionModel


def evaluate(model: FusionModel, split: tuple[Any, ...]) -> dict[str, float]:
    probabilities, _ = model.forward(split[0], split[1])
    return binary_metrics(probabilities, split[2])
