"""Loopback reranker service client with explicit profiles."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

import httpx


class RerankerClient:
    def __init__(
        self,
        endpoint: str,
        *,
        api_key: str = "",
        timeout_seconds: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.endpoint = endpoint.rstrip("/")
        self._client = httpx.Client(
            base_url=self.endpoint,
            headers=headers,
            timeout=timeout_seconds,
            transport=transport,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def health(self) -> bool:
        try:
            return self._client.get("/health").status_code == 200
        except httpx.HTTPError:
            return False

    def rerank(
        self,
        query: str,
        candidates: Sequence[str] | Sequence[Mapping[str, Any]],
        *,
        profile: str,
    ) -> list[float]:
        response = self._client.post(
            "/rerank",
            json={
                "query": query,
                "passages": [
                    str(value.get("text") or "") if isinstance(value, Mapping) else str(value)
                    for value in candidates
                ],
                "profile": profile,
            },
        )
        response.raise_for_status()
        payload = response.json()
        scores = payload.get("scores") if isinstance(payload, Mapping) else None
        if not isinstance(scores, list) or len(scores) != len(candidates):
            raise RuntimeError("reranker returned an unexpected score shape")
        return [float(value) for value in scores]
