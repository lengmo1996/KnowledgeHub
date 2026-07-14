"""Unified manifest-to-index pipeline with lazy orchestration imports."""

from __future__ import annotations

from typing import Any

from knowledgehub.pipeline.config import GPUPlan, RagConfig

__all__ = ["GPUPlan", "PipelineOrchestrator", "PipelineSummary", "RagConfig"]


def __getattr__(name: str) -> Any:
    if name in {"PipelineOrchestrator", "PipelineSummary"}:
        from knowledgehub.pipeline.orchestrator import (
            PipelineOrchestrator,
            PipelineSummary,
        )

        return {
            "PipelineOrchestrator": PipelineOrchestrator,
            "PipelineSummary": PipelineSummary,
        }[name]
    raise AttributeError(name)
