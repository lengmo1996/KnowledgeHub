"""Read-only environment inspection for deployment readiness."""

from __future__ import annotations

import importlib.metadata
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from knowledgehub.pipeline.config import RagConfig, inspect_gpu_devices


def inspect_environment(config: RagConfig) -> dict[str, Any]:
    packages: dict[str, str | None] = {}
    for name in (
        "docling",
        "PyMuPDF",
        "transformers",
        "qdrant-client",
        "fastembed",
        "pyarrow",
        "torch",
    ):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    devices = inspect_gpu_devices()
    docker = _command_version(["docker", "--version"])
    compose = _command_version(["docker", "compose", "version"])
    ready = 0
    if config.source_snapshot_path.is_file():
        with config.source_snapshot_path.open("r", encoding="utf-8") as stream:
            import json

            for line in stream:
                try:
                    value = json.loads(line)
                    ready += int(value.get("status") == "ready")
                except (json.JSONDecodeError, AttributeError):
                    pass
    endpoints = {
        endpoint: _port_open(endpoint)
        for endpoint in (*config.embedding_endpoints, config.qdrant_url, config.reranker_url)
    }
    return {
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "packages": packages,
        "gpus": [
            {
                "logical_id": value.logical_id,
                "name": value.name,
                "total_memory_mb": value.total_memory_mb,
                "free_memory_mb": value.free_memory_mb,
                "uuid": value.uuid,
                "pci_bus_id": value.pci_bus_id,
            }
            for value in devices
        ],
        "gpu_plan": config.resolve_gpu_plan(devices).to_dict(),
        "docker": docker,
        "docker_compose": compose,
        "source_snapshot": str(config.source_snapshot_path),
        "source_snapshot_exists": config.source_snapshot_path.is_file(),
        "source_ready_documents": ready,
        "data_dir": _path_status(config.data_dir),
        "model_cache_dir": _path_status(config.model_cache_dir),
        "endpoints": endpoints,
    }


def _command_version(command: list[str]) -> str | None:
    if shutil.which(command[0]) is None:
        return None
    completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=10)
    return completed.stdout.strip() or completed.stderr.strip() or None


def _path_status(path: Path) -> dict[str, Any]:
    parent = (
        path
        if path.exists()
        else next((candidate for candidate in path.parents if candidate.exists()), path)
    )
    return {
        "path": str(path),
        "exists": path.exists(),
        "writable_parent": parent.is_dir() and os_access_write(parent),
        "free_bytes": shutil.disk_usage(parent).free if parent.exists() else None,
    }


def os_access_write(path: Path) -> bool:
    import os

    return os.access(path, os.W_OK | os.X_OK)


def _port_open(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.hostname is None or parsed.port is None:
        return False
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=0.25):
            return True
    except OSError:
        return False
