"""Repository intake, API inventory and conservative compatibility matrix."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version

from knowledgehub.core.atomic import atomic_write_json, atomic_write_text

_FILES = ("pyproject.toml", "requirements.txt", "environment.yml", "environment.yaml", "setup.py", "setup.cfg", "Dockerfile")


@dataclass(frozen=True, slots=True)
class RepositoryIntake:
    root: Path

    def analyze(self, environment: Mapping[str, Any], output_root: Path) -> dict[str, Any]:
        root = self.root.resolve(strict=True)
        dependencies = self._dependencies(root)
        api = self._api_inventory(root)
        profile = {
            "schema_name": "repository_profile",
            "schema_version": "2.0",
            "repository": root.name,
            "root": str(root),
            "configuration_files": [name for name in _FILES if (root / name).is_file()],
            "entrypoints": [path.relative_to(root).as_posix() for path in sorted(root.glob("*.py"))],
            "training_scripts": [path.relative_to(root).as_posix() for path in sorted(root.rglob("*train*.py"))[:50]],
            "inference_scripts": [path.relative_to(root).as_posix() for path in sorted(root.rglob("*infer*.py"))[:50]],
            "tests": [path.relative_to(root).as_posix() for path in sorted(root.rglob("test_*.py"))[:200]],
            "dependencies": dependencies,
            "api_usage": api,
        }
        packages = environment.get("packages") or {}
        matrix = [self._compatibility(item, packages) for item in dependencies]
        destination = output_root / root.name
        atomic_write_json(destination / "repository_profile.json", profile)
        atomic_write_json(destination / "compatibility_matrix.json", {"environment": environment.get("name"), "rows": matrix})
        report = self._report(profile, matrix, str(environment.get("name") or "unknown"))
        atomic_write_text(destination / "compatibility_report.md", report)
        return {"profile": profile, "compatibility_matrix": matrix, "report": str(destination / "compatibility_report.md")}

    @staticmethod
    def _dependencies(root: Path) -> list[dict[str, str]]:
        result: dict[str, dict[str, str]] = {}
        path = root / "requirements.txt"
        if path.is_file():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith(("#", "-")):
                    continue
                try:
                    requirement = Requirement(line)
                except InvalidRequirement:
                    continue
                result[requirement.name.lower()] = {"package": requirement.name, "requirement": str(requirement.specifier), "source": "requirements.txt", "evidence_kind": "declared"}
        pyproject = root / "pyproject.toml"
        if pyproject.is_file():
            for match in re.finditer(r"['\"]([A-Za-z0-9_.-]+)([^'\"]*)['\"]", pyproject.read_text(encoding="utf-8", errors="replace")):
                try:
                    requirement = Requirement("".join(match.groups()))
                except InvalidRequirement:
                    continue
                result.setdefault(requirement.name.lower(), {"package": requirement.name, "requirement": str(requirement.specifier), "source": "pyproject.toml", "evidence_kind": "declared"})
        return sorted(result.values(), key=lambda item: item["package"].lower())

    @staticmethod
    def _api_inventory(root: Path) -> list[dict[str, Any]]:
        values: dict[str, dict[str, Any]] = {}
        for path in sorted(root.rglob("*.py")):
            if any(part.startswith(".") or part in {"venv", ".venv", "build"} for part in path.parts):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, SyntaxError):
                continue
            relative = path.relative_to(root).as_posix()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                else:
                    continue
                for name in names:
                    library = name.split(".")[0]
                    item = values.setdefault(library, {"library": library, "imports": set(), "symbols": set(), "files": set(), "call_sites": []})
                    item["imports"].add(name)
                    item["files"].add(relative)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = ast.unparse(node.func)
                    library = name.split(".")[0]
                    if library in values:
                        values[library]["symbols"].add(name)
                        values[library]["call_sites"].append({"file": relative, "line": node.lineno, "symbol": name, "parameters": [keyword.arg for keyword in node.keywords if keyword.arg]})
        return [{**value, "imports": sorted(value["imports"]), "symbols": sorted(value["symbols"]), "files": sorted(value["files"]), "call_sites": value["call_sites"][:500]} for value in values.values()]

    @staticmethod
    def _compatibility(dependency: Mapping[str, str], packages: Mapping[str, Any]) -> dict[str, Any]:
        package = dependency["package"]
        current = packages.get(package) or packages.get(package.lower())
        requirement = dependency["requirement"]
        if current is None:
            status, reason = "unknown", "package is not present in the selected environment profile"
        elif not requirement:
            status, reason = "unknown", "repository does not declare a version range"
        else:
            try:
                status = "likely_compatible" if Requirement(f"{package}{requirement}").specifier.contains(Version(str(current).split("+")[0])) else "conflict"
                reason = "declared requirement comparison; runtime behavior is not yet verified"
            except (InvalidRequirement, InvalidVersion):
                status, reason = "unknown", "version value could not be normalized"
        return {**dict(dependency), "environment_version": current, "status": status, "basis": reason}

    @staticmethod
    def _report(profile: Mapping[str, Any], matrix: list[dict[str, Any]], environment: str) -> str:
        lines = [f"# Compatibility report: {profile['repository']}", "", f"Target environment: `{environment}`", "", "## Dependency matrix", "", "| Package | Repository requirement | Environment | Status | Basis |", "| --- | --- | --- | --- | --- |"]
        lines.extend(f"| {row['package']} | {row['requirement'] or 'unspecified'} | {row['environment_version'] or 'missing'} | {row['status']} | {row['basis']} |" for row in matrix)
        lines.extend(["", "## Verification boundary", "", "Statuses are based on declarations only. `likely_compatible` is not a runtime guarantee. Query Code RAG for each affected symbol before editing, then run repository tests and record evidence in `adaptation_log.md`.", ""])
        return "\n".join(lines)
