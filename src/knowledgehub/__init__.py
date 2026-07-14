"""KnowledgeHub data ingestion package."""

from __future__ import annotations

try:
    from importlib.metadata import version

    __version__ = version("knowledgehub")
except Exception:  # pragma: no cover - package metadata is optional in a source checkout
    __version__ = "0.1.0"

__all__ = ["__version__"]
