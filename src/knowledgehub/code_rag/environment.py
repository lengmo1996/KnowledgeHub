"""Sanitized, reproducible local Python environment snapshots."""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, distributions, version
from pathlib import Path
from typing import Any, Sequence

from knowledgehub.core.atomic import atomic_write_json
from knowledgehub.core.hashing import sha256_file

_CREDENTIAL = re.compile(r"(?P<scheme>https?://)(?:[^/@\s]+)@", re.I)
_SECRET_QUERY = re.compile(r"([?&](?:token|key|password|secret)=)[^&\s]+", re.I)
_PROJECT_FILES = (
    "pyproject.toml",
    "requirements.txt",
    "environment.yml",
    "environment.yaml",
    "poetry.lock",
    "uv.lock",
)


def _redact(value: str) -> str:
    value = _CREDENTIAL.sub(r"\g<scheme><redacted>@", value)
    return _SECRET_QUERY.sub(r"\1<redacted>", value)


class EnvironmentCapture:
    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root

    def capture(
        self,
        *,
        name: str = "current",
        project: Path | None = None,
        packages: Sequence[str] = (),
        dry_run: bool = False,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise ValueError("environment name contains unsupported characters")
        installed: dict[str, str] = {}
        for dist in distributions():
            package_name = dist.metadata["Name"]
            if package_name:
                installed[package_name.lower()] = dist.version
        selected = {item: installed.get(item.lower()) for item in packages} if packages else installed
        pip_list = self._pip("list", "--format=json")
        pip_freeze = [_redact(line) for line in self._pip("freeze").splitlines() if line.strip()]
        project_files: list[dict[str, Any]] = []
        root = (project or Path.cwd()).expanduser().resolve()
        for filename in _PROJECT_FILES:
            path = root / filename
            if path.is_file():
                project_files.append(
                    {"path": filename, "sha256": sha256_file(path), "size_bytes": path.stat().st_size}
                )
        snapshot = {
            "schema_version": 1,
            "name": name,
            "python_version": platform.python_version(),
            "python_executable": sys.executable,
            "platform": platform.platform(),
            "packages": dict(sorted(selected.items())),
            "pip_list": json.loads(pip_list or "[]"),
            "pip_freeze": pip_freeze,
            "project_root": str(root),
            "project_files": project_files,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "source": "pip",
        }
        output = self.data_root / "state" / "environments" / f"{name}.json"
        snapshot["output"] = str(output)
        if not dry_run:
            atomic_write_json(output, snapshot, mode=0o600)
        return snapshot

    @staticmethod
    def _pip(*arguments: str) -> str:
        completed = subprocess.run(
            [sys.executable, "-m", "pip", *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
        )
        if completed.returncode != 0:
            return "[]" if "--format=json" in arguments else ""
        return _redact(completed.stdout)


def installed_version(package: str) -> str | None:
    try:
        return version(package)
    except PackageNotFoundError:
        return None
