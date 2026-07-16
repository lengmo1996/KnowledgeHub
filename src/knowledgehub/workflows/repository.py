"""Repository intake, API inventory and conservative compatibility matrix."""

from __future__ import annotations

import ast
import configparser
import json
import subprocess
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised by the supported Python 3.10 runtime
    import tomli as tomllib  # type: ignore[import-not-found]

import yaml
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from knowledgehub.core.atomic import atomic_write_json, atomic_write_text

_FILES = ("pyproject.toml", "requirements.txt", "environment.yml", "environment.yaml", "setup.py", "setup.cfg", "Dockerfile")


def _api_item(library: str) -> dict[str, Any]:
    return {
        "library": library,
        "imports": set(),
        "symbols": set(),
        "files": set(),
        "call_sites": [],
        "inherited_symbols": [],
        "monkey_patches": [],
        "detected_version_assumptions": [],
    }


def _resolved_expression(node: ast.AST, aliases: Mapping[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        return f"{_resolved_expression(node.value, aliases)}.{node.attr}"
    return ast.unparse(node)


@dataclass(frozen=True, slots=True)
class RepositoryIntake:
    root: Path

    def inspect(self, environment: Mapping[str, Any]) -> dict[str, Any]:
        """Build the intake result without writing files or executing repository code."""
        root = self.root.resolve(strict=True)
        dependencies = self._dependencies(root)
        python_paths = [
            path
            for path in sorted(root.rglob("*.py"))
            if not any(
                part.startswith(".") or part in {"venv", ".venv", "build"}
                for part in path.relative_to(root).parts
            )
        ]
        api_limit = 5_000
        api = self._api_inventory(root, python_paths[:api_limit])
        context = self._repository_context(root, dependencies, api)
        profile = {
            "schema_name": "repository_profile",
            "schema_version": "2.0",
            **self._identity(root),
            "root": str(root),
            **context,
            "entrypoints": [path.relative_to(root).as_posix() for path in sorted(root.glob("*.py"))],
            "training_scripts": [path.relative_to(root).as_posix() for path in sorted(root.rglob("*train*.py"))[:50]],
            "inference_scripts": [path.relative_to(root).as_posix() for path in sorted(root.rglob("*infer*.py"))[:50]],
            "tests": [path.relative_to(root).as_posix() for path in sorted(root.rglob("test_*.py"))[:200]],
            "dependencies": dependencies,
            "api_usage": api,
            "api_inventory": {
                "python_files_discovered": len(python_paths),
                "python_files_analyzed": min(len(python_paths), api_limit),
                "max_python_files": api_limit,
                "truncated": len(python_paths) > api_limit,
            },
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

    @staticmethod
    def _repository_context(
        root: Path,
        dependencies: Sequence[Mapping[str, str]],
        api_usage: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        configuration_files = {
            name for name in _FILES if (root / name).is_file()
        }
        configuration_files.update(
            path.relative_to(root).as_posix()
            for path in root.glob("requirements*.txt")
        )
        ci_files = [
            path.relative_to(root).as_posix()
            for path in sorted((root / ".github" / "workflows").glob("*.y*ml"))
        ][:100]
        dockerfiles = [
            path.relative_to(root).as_posix()
            for path in sorted(root.rglob("Dockerfile*"))
            if ".git" not in path.parts
        ][:50]
        configuration_files.update(ci_files)
        configuration_files.update(dockerfiles)

        python_declarations: list[dict[str, str]] = []
        pyproject = root / "pyproject.toml"
        if pyproject.is_file():
            try:
                project = (tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project") or {})
            except (OSError, tomllib.TOMLDecodeError):
                project = {}
            if project.get("requires-python"):
                python_declarations.append(
                    {
                        "requirement": str(project["requires-python"]),
                        "source": "pyproject.toml:project.requires-python",
                    }
                )

        packages = {canonicalize_name(item["package"]) for item in dependencies}
        imported = {item["library"] for item in api_usage}
        systems = []
        for name, indicators in {
            "hydra": {"hydra-core", "hydra", "omegaconf"},
            "argparse": {"argparse"},
            "click": {"click", "typer"},
            "pydantic": {"pydantic"},
        }.items():
            if indicators & (packages | imported) or (name == "hydra" and (root / "configs").is_dir()):
                systems.append(name)

        inspected_files = [
            root / "README.md",
            root / "requirements.txt",
            root / "environment.yml",
            root / "environment.yaml",
            *(root / value for value in dockerfiles),
        ]
        runtime_text = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")[:200_000]
            for path in inspected_files
            if path.is_file()
        ).lower()
        target_hardware = [
            value
            for value in ("cuda", "gpu", "rocm", "mps", "tpu")
            if value in runtime_text
        ]
        custom_extensions = [
            path.relative_to(root).as_posix()
            for path in sorted(root.rglob("*"))
            if path.is_file()
            and path.suffix.lower() in {".c", ".cc", ".cpp", ".cu", ".cuh", ".so"}
            and ".git" not in path.parts
        ][:100]
        return {
            "configuration_files": sorted(configuration_files),
            "ci_files": ci_files,
            "dockerfiles": dockerfiles,
            "python_declarations": python_declarations,
            "configuration_systems": systems,
            "target_hardware": target_hardware,
            "custom_extensions": custom_extensions,
        }

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

        requirement_paths = sorted(
            {
                *root.glob("requirements*.txt"),
                *root.glob("requirements/**/*.txt"),
            }
        )[:100]
        for path in requirement_paths:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.split(" #", 1)[0].strip()
                if not line or line.startswith(("#", "-")):
                    continue
                add(line, path.relative_to(root).as_posix())
        for name in ("environment.yml", "environment.yaml"):
            environment_path = root / name
            if not environment_path.is_file():
                continue
            try:
                environment = yaml.safe_load(environment_path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                environment = {}
            for dependency in environment.get("dependencies") or ():
                if isinstance(dependency, str):
                    # Conda's single-equals form is normalized for PEP 440 parsing.
                    normalized = dependency
                    if "=" in dependency and not any(
                        operator in dependency
                        for operator in ("==", ">=", "<=", "!=", "~=", "===")
                    ):
                        normalized = dependency.replace("=", "==", 1)
                    add(normalized, name)
                elif isinstance(dependency, Mapping):
                    for requirement in dependency.get("pip") or ():
                        add(str(requirement), f"{name}:pip")
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
        setup_cfg = root / "setup.cfg"
        if setup_cfg.is_file():
            parser = configparser.ConfigParser()
            try:
                parser.read(setup_cfg, encoding="utf-8")
                raw_requirements = parser.get("options", "install_requires", fallback="")
            except (OSError, configparser.Error):
                raw_requirements = ""
            for requirement in raw_requirements.splitlines():
                if requirement.strip():
                    add(requirement.strip(), "setup.cfg:options.install_requires")
        setup = root / "setup.py"
        if setup.is_file():
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", SyntaxWarning)
                    tree = ast.parse(
                        setup.read_text(encoding="utf-8", errors="replace")
                    )
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
        commit = RepositoryIntake._git_value(root, "rev-parse", "HEAD")
        remote = RepositoryIntake._git_value(root, "remote", "get-url", "origin")
        safe_remote = remote if remote and (remote.startswith("https://github.com/") or remote.startswith("git@github.com:")) else None
        slug = None
        if safe_remote:
            slug = safe_remote.removeprefix("https://github.com/").removeprefix(
                "git@github.com:"
            ).removesuffix(".git")
        repository = slug.split("/")[-1] if slug else root.name
        return {
            "repository": repository,
            "repository_key": slug.replace("/", "--") if slug else root.name,
            "repository_url": safe_remote,
            "commit": commit,
        }

    @staticmethod
    def _git_value(root: Path, *args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    @staticmethod
    def _api_inventory(root: Path, paths: Sequence[Path]) -> list[dict[str, Any]]:
        values: dict[str, dict[str, Any]] = {}
        for path in paths:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", SyntaxWarning)
                    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, SyntaxError):
                continue
            relative = path.relative_to(root).as_posix()
            aliases: dict[str, str] = {}
            for node in ast.walk(tree):
                imports: list[tuple[str, str]] = []
                if isinstance(node, ast.Import):
                    imports = [
                        (alias.asname or alias.name.split(".")[0], alias.name)
                        for alias in node.names
                    ]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports = [
                        (
                            alias.asname or alias.name,
                            f"{node.module}.{alias.name}",
                        )
                        for alias in node.names
                        if alias.name != "*"
                    ]
                for local, imported in imports:
                    aliases[local] = imported
                    library = imported.split(".")[0]
                    item = values.setdefault(library, _api_item(library))
                    item["imports"].add(imported)
                    item["files"].add(relative)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = _resolved_expression(node.func, aliases)
                    library = name.split(".")[0]
                    if library in values:
                        values[library]["symbols"].add(name)
                        values[library]["call_sites"].append(
                            {
                                "file": relative,
                                "line": node.lineno,
                                "symbol": name,
                                "parameters": [
                                    keyword.arg for keyword in node.keywords if keyword.arg
                                ],
                                "positional_arguments": len(node.args),
                            }
                        )
                elif isinstance(node, ast.ClassDef):
                    for base in node.bases:
                        name = _resolved_expression(base, aliases)
                        library = name.split(".")[0]
                        if library in values:
                            values[library]["inherited_symbols"].append(
                                {
                                    "file": relative,
                                    "line": node.lineno,
                                    "class": node.name,
                                    "base": name,
                                }
                            )
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        if not isinstance(target, ast.Attribute):
                            continue
                        name = _resolved_expression(target, aliases)
                        library = name.split(".")[0]
                        if library in values:
                            values[library]["monkey_patches"].append(
                                {"file": relative, "line": node.lineno, "target": name}
                            )
                elif isinstance(node, ast.Compare):
                    expression = ast.unparse(node)
                    for local, imported in aliases.items():
                        if f"{local}.__version__" not in expression:
                            continue
                        library = imported.split(".")[0]
                        values[library]["detected_version_assumptions"].append(
                            {"file": relative, "line": node.lineno, "expression": expression}
                        )
        return [
            {
                **value,
                "imports": sorted(value["imports"]),
                "symbols": sorted(value["symbols"]),
                "files": sorted(value["files"]),
                "call_sites": value["call_sites"][:500],
                "inherited_symbols": value["inherited_symbols"][:200],
                "monkey_patches": value["monkey_patches"][:200],
                "detected_version_assumptions": value["detected_version_assumptions"][:200],
            }
            for value in sorted(values.values(), key=lambda item: item["library"])
        ]

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
        lines = [
            f"# Compatibility report: {profile['repository']}",
            "",
            f"Target environment: `{environment}`",
            "",
            "## Repository overview",
            "",
            f"- Configuration systems: {', '.join(profile.get('configuration_systems') or []) or 'not detected'}",
            f"- Entrypoints: {len(profile.get('entrypoints') or [])}",
            f"- Training scripts: {len(profile.get('training_scripts') or [])}",
            f"- Inference scripts: {len(profile.get('inference_scripts') or [])}",
            f"- Tests discovered: {len(profile.get('tests') or [])}",
            f"- Target hardware indicators: {', '.join(profile.get('target_hardware') or []) or 'none'}",
            "",
            "## Dependency matrix",
            "",
            "| Package | Repository requirement | Environment | Status | Basis |",
            "| --- | --- | --- | --- | --- |",
        ]
        lines.extend(
            f"| {row['package']} | {row['requirement'] or 'unspecified'} | {row['environment_version'] or 'missing'} | {row['status']} | {row['basis']} |"
            for row in matrix
        )
        lines.extend(
            [
                "",
                "## Affected external APIs",
                "",
                "| Library | Symbols/calls | Files | Version assumptions |",
                "| --- | --- | ---: | ---: |",
            ]
        )
        for item in profile.get("api_usage") or []:
            symbols = ", ".join(str(value).replace("|", "\\|") for value in item["symbols"][:8])
            lines.append(
                f"| {item['library']} | {symbols or 'imports only'} | {len(item['files'])} | {len(item.get('detected_version_assumptions') or [])} |"
            )
        conflicts = [row for row in matrix if row["status"] == "conflict"]
        unknown = [row for row in matrix if row["status"] == "unknown"]
        lines.extend(["", "## Suggested adaptation plan", ""])
        if conflicts:
            lines.extend(
                f"- Review `{row['package']}`: declared `{row['requirement'] or 'unspecified'}`, environment `{row['environment_version'] or 'missing'}`."
                for row in conflicts
            )
        else:
            lines.append("- No declaration-level conflicts were detected; continue with symbol-level checks.")
        lines.extend(
            [
                "- Build one evidence package per proposed API/configuration change before editing.",
                "- Prefer compatibility shims or configuration migration over changing the user environment.",
                "",
                "## Modification risks",
                "",
                "- Optional and development dependencies may appear in declaration files; scope each conflict before changing runtime code.",
                "- Static call detection does not prove that a branch executes in the selected environment.",
                "- A satisfied version range is only `likely_compatible`, never a runtime guarantee.",
                "",
                "## Verification plan",
                "",
                "1. Run syntax/static checks on affected files.",
                "2. Run the smallest repository-owned test covering each changed symbol or configuration.",
                "3. Run a bounded CPU/GPU smoke path only when its dependencies and data are already trusted and available.",
                "4. Record commands, exit codes, output hashes and unresolved risks in `adaptation_log.md`.",
                "",
                "## Unconfirmed items",
                "",
            ]
        )
        lines.extend(
            [f"- `{row['package']}`: {row['basis']}" for row in unknown]
            or ["- No unknown dependency rows."]
        )
        lines.extend(
            [
                "",
                "## Verification boundary",
                "",
                "This report is declaration and static-AST evidence. Query Code RAG for each affected symbol before editing; only executed checks recorded in `adaptation_log.md` count as verification.",
                "",
            ]
        )
        return "\n".join(lines)
