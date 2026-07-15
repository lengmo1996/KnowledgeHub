"""Thread-safe TEI endpoint selection, retry, and quarantine."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Sequence

from knowledgehub.embeddings.models import EmbeddingBatchResult
from knowledgehub.embeddings.tei_client import EmbeddingServiceError, TEIClient


@dataclass(slots=True)
class _EndpointState:
    client: TEIClient
    outstanding: int = 0
    failures: int = 0
    quarantined_until: float = 0.0
    batches: int = 0
    texts: int = 0
    latency_seconds: float = 0.0
    latencies: list[float] = field(default_factory=list)


class EndpointPool:
    def __init__(
        self,
        clients: Sequence[TEIClient],
        *,
        strategy: str = "least_outstanding",
        max_attempts: int = 3,
        quarantine_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not clients:
            raise ValueError("endpoint pool requires at least one client")
        if strategy not in {"round_robin", "least_outstanding"}:
            raise ValueError("invalid endpoint strategy")
        self._states = [_EndpointState(client=value) for value in clients]
        self.strategy = strategy
        self.max_attempts = max_attempts
        self.quarantine_seconds = quarantine_seconds
        self.clock = clock
        self._lock = threading.Lock()
        self._cursor = 0

    @classmethod
    def create(
        cls,
        endpoints: Sequence[str],
        *,
        output_dim: int,
        normalize: bool,
        timeout_seconds: float,
        strategy: str = "least_outstanding",
        api_key: str = "",
    ) -> "EndpointPool":
        return cls(
            [
                TEIClient(
                    endpoint,
                    output_dim=output_dim,
                    normalize=normalize,
                    timeout_seconds=timeout_seconds,
                    api_key=api_key,
                )
                for endpoint in endpoints
            ],
            strategy=strategy,
        )

    def close(self) -> None:
        for state in self._states:
            state.client.close()

    def embed(self, texts: Sequence[str]) -> EmbeddingBatchResult:
        errors: list[str] = []
        tried: set[str] = set()
        for _ in range(max(self.max_attempts, len(self._states))):
            state = self._acquire(exclude=tried)
            tried.add(state.client.endpoint)
            try:
                result = state.client.embed(texts)
            except EmbeddingServiceError as exc:
                errors.append(str(exc))
                self._release(state, failed=True, texts=0, latency=0.0)
                if len(tried) == len(self._states):
                    tried.clear()
                continue
            self._release(
                state,
                failed=False,
                texts=result.text_count,
                latency=result.latency_seconds,
            )
            return result
        raise EmbeddingServiceError("all TEI endpoints failed: " + "; ".join(errors))

    def stats(self) -> dict[str, dict[str, float | int]]:
        with self._lock:
            return {
                value.client.endpoint: {
                    "batches": value.batches,
                    "failures": value.failures,
                    "latency_seconds": round(value.latency_seconds, 6),
                    "p95_latency_seconds": _percentile(value.latencies, 0.95),
                    "outstanding": value.outstanding,
                    "texts": value.texts,
                }
                for value in self._states
            }

    def health(self) -> dict[str, bool]:
        return {value.client.endpoint: value.client.health() for value in self._states}

    def _acquire(self, *, exclude: set[str]) -> _EndpointState:
        with self._lock:
            now = self.clock()
            eligible = [
                value
                for value in self._states
                if value.client.endpoint not in exclude and value.quarantined_until <= now
            ]
            if not eligible:
                eligible = [value for value in self._states if value.client.endpoint not in exclude]
            if not eligible:
                eligible = list(self._states)
            if self.strategy == "round_robin":
                state = eligible[self._cursor % len(eligible)]
                self._cursor += 1
            else:
                # Sequential coordinators still need to exercise both replicas.
                # Completed batch count provides a deterministic tie-breaker
                # when all endpoints currently have zero outstanding requests.
                state = min(
                    eligible,
                    key=lambda value: (
                        value.outstanding,
                        value.batches,
                        value.failures,
                        value.client.endpoint,
                    ),
                )
            state.outstanding += 1
            return state

    def _release(
        self,
        state: _EndpointState,
        *,
        failed: bool,
        texts: int,
        latency: float,
    ) -> None:
        with self._lock:
            state.outstanding = max(0, state.outstanding - 1)
            if failed:
                state.failures += 1
                state.quarantined_until = self.clock() + self.quarantine_seconds
            else:
                state.batches += 1
                state.texts += texts
                state.latency_seconds += latency
                state.latencies.append(latency)


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * percentile) - 1)
    return round(float(ordered[index]), 6)
