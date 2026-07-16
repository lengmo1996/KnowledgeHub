"""Repository intake, API inventory and conservative compatibility matrix."""

from __future__ import annotations

import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised by the supported Python 3.10 runtime
    import tomli as tomllib  # type: ignore[import-not-found]

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from knowledgehub.core.atomic import atomic_write_json, atomic_write_text

_FILES = ("pyproject.toml", "requirements.txt", "environment.yml", "environment.yaml", "setup.py", "setup.cfg", "Dockerfile")


@dataclass(frozen=True, slots=True)
class RepositoryIntake:
    root: Path

    def inspect(self, environment: Mapping[str, Any]) -> dict[str, Any]:
        """Build the intake result without writing files or executing repository code."""
        root = self.root.resolve(strict=True)
        dependencies = self._dependencies(root)
        api = self._api_inventory(root)
        profile = {
            "schema_name": "repository_profile",
            "schema_version": "2.0",
            **self._identity(root),
            "root": str(root),
            "configuration_files": [name for name in _FILES if (root / name).is_file()],
            "entrypoints": [path.relative_to(root).as_posix() for path in sorted(root.glob("*.py"))],
            "training_scripts": [path.relative_to(root).as_posix() for path in sorted(root.rglob("*train*.py"))[:50]],
            "inference_scripts": [path.relative_to(root).as_posix() for path in sorted(root.rglob("*infer*.py"))[:50]],
            "tests": [path.relative_to(root).as_posix() for path in sorted(root.rglob("test_*.py"))[:200]],
            "dependencies": dependencies,
            "api_usage": api,
        }
        packages = dict(environment.get("packages") or {})
        if not packages.get("python"):
            packages["python"] = environment.get("python_version")
        packages.update(
            {
                canonicalize_name(str(item["name"])): item["version"]
                for item in environment.get("pip_list") or ()
                if isinstance(item, Mapping) and item.get("name") and item.get("version")
            }
        )
        matrix = [self._compatibility(item, packages) for item in dependencies]
        return {"profile": profile, "compatibility_matrix": matrix}

    def analyze(self, environment: Mapping[str, Any], output_root: Path) -> dict[str, Any]:
        result = self.inspect(environment)
        profile = result["profile"]
        matrix = result["compatibility_matrix"]
        destination = output_root / str(profile["repository_key"])
        atomic_write_json(destination / "repository_profile.json", profile)
        atomic_write_json(destination / "compatibility_matrix.json", {"environment": environment.get("name"), "rows": matrix})
        report = self._report(profile, matrix, str(environment.get("name") or "unknown"))
        atomic_write_text(destination / "compatibility_report.md", report)
        return {**result, "report": str(destination / "compatibility_report.md")}

    @staticmethod
    def _dependencies(root: Path) -> list[dict[str, str]]:
        result: dict[str, dict[str, str]] = {}

        def add(value: str, source: str) -> None:
            try:
                requirement = Requirement(value)
            except InvalidRequirement:
                return
            if requirement.marker is not None and not requirement.marker.evaluate():
                return
            result[canonicalize_name(requirement.name)] = {
                "package": requirement.name,
                "requirement": str(requirement.specifier),
                "source": source,
                "evidence_kind": "declared",
            }

        path = root / "requirements.txt"
        if path.is_file():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith(("#", "-")):
                    continue
                add(line, "requirements.txt")
        pyproject = root / "pyproject.toml"
        if pyproject.is_file():
            try:
                value = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError):
                value = {}
            project = value.get("project") or {}
            for requirement in project.get("dependencies") or ():
                add(str(requirement), "pyproject.toml:project.dependencies")
            for group, requirements in (project.get("optional-dependencies") or {}).items():
                for requirement in requirements or ():
                    add(str(requirement), f"pyproject.toml:project.optional-dependencies.{group}")
            for group, requirements in (value.get("dependency-groups") or {}).items():
                for requirement in requirements or ():
                    if isinstance(requirement, str):
                        add(requirement, f"pyproject.toml:dependency-groups.{group}")
            for requirement in (value.get("build-system") or {}).get("requires") or ():
                add(str(requirement), "pyproject.toml:build-system.requires")
        setup = root / "setup.py"
        if setup.is_file():
            try:
                tree = ast.parse(setup.read_text(encoding="utf-8", errors="replace"))
            except (OSError, SyntaxError):
                tree = ast.Module(body=[], type_ignores=[])
            for node in tree.body:
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if not any(
                    isinstance(target, ast.Name) and target.id == "_deps"
                    for target in targets
                ):
                    continue
                if node.value is None:
                    continue
                try:
                    dependencies = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    continue
                for requirement in dependencies if isinstance(dependencies, list) else ():
                    if isinstance(requirement, str):
                        add(requirement, "setup.py:_deps")
        return sorted(result.values(), key=lambda item: item["package"].lower())

    @staticmethod
    def _identity(root: Path) -> dict[str, Any]:
        marker = root.parent / "current.json"
        if marker.is_file():
            try:
                value = json.loads(marker.read_text(encoding="utf-8"))
                if Path(str(value.get("source_path"))).resolve() == root:
                    library = str(value.get("library") or root.name)
                    version = str(value.get("version") or "unknown")
                    return {
                        "repository": library,
                        "repository_key": f"{library}-{version}",
                        "version": version,
                        "commit": value.get("commit"),
                    }
            except (OSError, ValueError):
                pass
        return {"repository": root.name, "repository_key": root.name}

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
        current = packages.get(package) or packages.get(canonicalize_name(package))
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
