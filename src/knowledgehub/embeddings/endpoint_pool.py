"""Bounded TEI endpoint scheduling with quarantine and failover."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Sequence

from knowledgehub.embeddings.models import EmbeddingBatchResult
from knowledgehub.embeddings.tei_client import TEIClient


@dataclass(slots=True)
class _EndpointState:
    client: TEIClient
    outstanding: int = 0
    failures: int = 0
    quarantined: bool = False
    batches: int = 0
    texts: int = 0


class EndpointPool:
    def __init__(
        self,
        clients: Sequence[TEIClient],
        *,
        strategy: str = "least_outstanding",
        max_failures: int = 2,
    ) -> None:
        if not clients:
            raise ValueError("endpoint pool requires at least one client")
        if strategy not in {"round_robin", "least_outstanding"}:
            raise ValueError("unsupported endpoint strategy")
        self._states = [_EndpointState(client=value) for value in clients]
        self.strategy = strategy
        self.max_failures = max_failures
        self._lock = threading.Lock()
        self._round_robin = 0

    @classmethod
    def create(
        cls,
        endpoints: Sequence[str],
        *,
        output_dim: int,
        normalize: bool,
        timeout_seconds: float,
        strategy: str,
        factory: Callable[..., TEIClient] = TEIClient,
    ) -> "EndpointPool":
        return cls(
            [
                factory(
                    endpoint,
                    output_dim=output_dim,
                    normalize=normalize,
                    timeout_seconds=timeout_seconds,
                )
                for endpoint in endpoints
            ],
            strategy=strategy,
        )

    def close(self) -> None:
        for state in self._states:
            state.client.close()

    def health(self) -> dict[str, bool]:
        return {state.client.endpoint: state.client.health() for state in self._states}

    def embed(self, texts: Sequence[str]) -> EmbeddingBatchResult:
        attempted: set[str] = set()
        last_error: Exception | None = None
        while len(attempted) < len(self._states):
            state = self._acquire(attempted)
            attempted.add(state.client.endpoint)
            try:
                result = state.client.embed(texts)
            except Exception as exc:
                last_error = exc
                with self._lock:
                    state.outstanding -= 1
                    state.failures += 1
                    state.quarantined = state.failures >= self.max_failures
                continue
            with self._lock:
                state.outstanding -= 1
                state.failures = 0
                state.batches += 1
                state.texts += len(texts)
            return result
        raise RuntimeError("all embedding endpoints failed") from last_error

    def stats(self) -> dict[str, dict[str, int | bool]]:
        with self._lock:
            return {
                state.client.endpoint: {
                    "batches": state.batches,
                    "texts": state.texts,
                    "failures": state.failures,
                    "outstanding": state.outstanding,
                    "quarantined": state.quarantined,
                }
                for state in self._states
            }

    def _acquire(self, excluded: set[str]) -> _EndpointState:
        with self._lock:
            eligible = [
                state
                for state in self._states
                if not state.quarantined and state.client.endpoint not in excluded
            ]
            if not eligible:
                # A request is allowed to probe quarantined endpoints once all
                # healthy endpoints have been attempted.
                eligible = [
                    state for state in self._states if state.client.endpoint not in excluded
                ]
            if not eligible:
                raise RuntimeError("no embedding endpoint is available")
            if self.strategy == "round_robin":
                state = eligible[self._round_robin % len(eligible)]
                self._round_robin += 1
            else:
                state = min(eligible, key=lambda value: (value.outstanding, value.client.endpoint))
            state.outstanding += 1
            return state
