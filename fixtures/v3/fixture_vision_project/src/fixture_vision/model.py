"""A tiny trainable two-branch NumPy model."""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from fixture_vision.fusion import addition, concatenate


class FusionModel:
    def __init__(self, input_dim: int, hidden_dim: int, fusion: str, seed: int) -> None:
        if fusion not in {"addition", "concatenation_projection"}:
            raise ValueError(f"unsupported fusion: {fusion}")
        rng = np.random.default_rng(seed)
        scale = 0.25
        self.fusion = fusion
        self.w_a = rng.normal(scale=scale, size=(input_dim, hidden_dim))
        self.b_a = np.zeros(hidden_dim)
        self.w_b = rng.normal(scale=scale, size=(input_dim, hidden_dim))
        self.b_b = np.zeros(hidden_dim)
        self.w_projection = (
            rng.normal(scale=scale, size=(hidden_dim * 2, hidden_dim))
            if fusion == "concatenation_projection"
            else None
        )
        self.b_projection = np.zeros(hidden_dim) if self.w_projection is not None else None
        self.w_output = rng.normal(scale=scale, size=hidden_dim)
        self.b_output = np.zeros(1)

    @property
    def parameter_count(self) -> int:
        parameters = self.w_a.size + self.b_a.size + self.w_b.size + self.b_b.size
        parameters += self.w_output.size + self.b_output.size
        if self.w_projection is not None and self.b_projection is not None:
            parameters += self.w_projection.size + self.b_projection.size
        return int(parameters)

    def forward(
        self, x_a: NDArray[np.float64], x_b: NDArray[np.float64]
    ) -> tuple[NDArray[np.float64], dict[str, NDArray[np.float64]]]:
        h_a = np.tanh(x_a @ self.w_a + self.b_a)
        h_b = np.tanh(x_b @ self.w_b + self.b_b)
        joined = addition(h_a, h_b) if self.fusion == "addition" else concatenate(h_a, h_b)
        fused = (
            np.tanh(joined @ self.w_projection + self.b_projection)
            if self.w_projection is not None and self.b_projection is not None
            else joined
        )
        logits = fused @ self.w_output + self.b_output[0]
        probabilities = 1.0 / (1.0 + np.exp(-np.clip(logits, -30, 30)))
        return probabilities, {"h_a": h_a, "h_b": h_b, "joined": joined, "fused": fused}

    def train_step(
        self,
        x_a: NDArray[np.float64],
        x_b: NDArray[np.float64],
        labels: NDArray[np.float64],
        learning_rate: float,
    ) -> float:
        probabilities, cache = self.forward(x_a, x_b)
        epsilon = 1e-8
        loss = -np.mean(
            labels * np.log(probabilities + epsilon)
            + (1 - labels) * np.log(1 - probabilities + epsilon)
        )
        d_logits = (probabilities - labels) / labels.size
        old_output = self.w_output.copy()
        gradient_output = cache["fused"].T @ d_logits
        gradient_output_bias = np.sum(d_logits)
        d_fused = np.outer(d_logits, old_output)
        if self.w_projection is not None and self.b_projection is not None:
            d_projection = d_fused * (1 - cache["fused"] ** 2)
            old_projection = self.w_projection.copy()
            self.w_projection -= learning_rate * (cache["joined"].T @ d_projection)
            self.b_projection -= learning_rate * np.sum(d_projection, axis=0)
            d_joined = d_projection @ old_projection.T
            split = cache["h_a"].shape[1]
            d_a, d_b = d_joined[:, :split], d_joined[:, split:]
        else:
            d_a = d_fused
            d_b = d_fused
        d_a *= 1 - cache["h_a"] ** 2
        d_b *= 1 - cache["h_b"] ** 2
        self.w_a -= learning_rate * (x_a.T @ d_a)
        self.b_a -= learning_rate * np.sum(d_a, axis=0)
        self.w_b -= learning_rate * (x_b.T @ d_b)
        self.b_b -= learning_rate * np.sum(d_b, axis=0)
        self.w_output -= learning_rate * gradient_output
        self.b_output[0] -= learning_rate * gradient_output_bias
        return float(loss)
