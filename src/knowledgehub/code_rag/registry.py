"""Configuration-driven registry and PEP 440 version selection."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Mapping, Sequence

import yaml
from packaging.version import InvalidVersion, Version


@dataclass(frozen=True, slots=True)
class CodeLibrary:
    name: str
    package_name: str
    repository: str
    enabled: bool
    version_strategy: tuple[str, ...]
    tag_patterns: tuple[str, ...]
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    max_file_bytes: int
    max_files: int
    max_chunks_per_file: int
    releases_enabled: bool
    release_limit: int
    issues_enabled: bool
    issue_limit: int

    def installed_version(self) -> str | None:
        try:
            return package_version(self.package_name)
        except PackageNotFoundError:
            return None


class CodeSourceRegistry:
    def __init__(self, path: Path, libraries: Mapping[str, CodeLibrary]) -> None:
        self.path = path
        self.libraries = dict(libraries)

    @classmethod
    def load(cls, path: Path | str) -> "CodeSourceRegistry":
        selected = Path(path).expanduser().resolve(strict=True)
        raw = yaml.safe_load(selected.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, Mapping) or int(raw.get("schema_version", 0)) != 1:
            raise ValueError("unsupported code source registry")
        defaults = raw.get("defaults") or {}
        items = raw.get("libraries") or {}
        if not isinstance(defaults, Mapping) or not isinstance(items, Mapping):
            raise ValueError("registry defaults and libraries must be mappings")
        libraries: dict[str, CodeLibrary] = {}
        for name, override in items.items():
            if not isinstance(override, Mapping):
                raise ValueError(f"library {name} must be a mapping")
            value = {**defaults, **override}
            releases = {**dict(defaults.get("releases") or {}), **dict(override.get("releases") or {})}
            issues = {**dict(defaults.get("issues") or {}), **dict(override.get("issues") or {})}
            library = CodeLibrary(
                name=str(name),
                package_name=str(value.get("package_name") or name),
                repository=str(value.get("repository") or ""),
                enabled=bool(value.get("enabled", False)),
                version_strategy=tuple(str(item) for item in value.get("version_strategy") or ()),
                tag_patterns=tuple(str(item) for item in value.get("tag_patterns") or ("v{version}",)),
                include=tuple(str(item) for item in value.get("include") or ()),
                exclude=tuple(str(item) for item in value.get("exclude") or ()),
                max_file_bytes=int(value.get("max_file_bytes", 2_000_000)),
                max_files=int(value.get("max_files", 20_000)),
                max_chunks_per_file=int(value.get("max_chunks_per_file", 500)),
                releases_enabled=bool(releases.get("enabled", False)),
                release_limit=int(releases.get("limit", 0)),
                issues_enabled=bool(issues.get("enabled", False)),
                issue_limit=int(issues.get("limit", 0)),
            )
            if not library.repository or not library.include:
                raise ValueError(f"library {name} has incomplete source configuration")
            libraries[library.name] = library
        return cls(selected, libraries)

    def get(self, name: str) -> CodeLibrary:
        try:
            return self.libraries[name]
        except KeyError as exc:
            raise ValueError(f"unknown code library: {name}") from exc

    def list(self, *, enabled_only: bool = False) -> list[CodeLibrary]:
        values = self.libraries.values()
        return sorted(
            (item for item in values if item.enabled or not enabled_only), key=lambda item: item.name
        )


def version_from_tag(tag: str) -> Version | None:
    candidate = tag.removeprefix("refs/tags/").removeprefix("v")
    try:
        parsed = Version(candidate)
    except InvalidVersion:
        return None
    return parsed if not parsed.is_prerelease and not parsed.is_devrelease else None


def select_versions(
    *,
    installed: str | None,
    available_tags: Sequence[str],
    strategies: Sequence[str],
    explicit: str | None = None,
) -> tuple[str, ...]:
    parsed: dict[Version, str] = {}
    for tag in available_tags:
        value = version_from_tag(tag)
        if value is not None:
            parsed[value] = tag.removeprefix("refs/tags/")
    ordered = sorted(parsed)
    result: list[Version] = []
    installed_value: Version | None = None
    if installed:
        try:
            installed_value = Version(installed.split("+")[0])
        except InvalidVersion:
            installed_value = None
    for strategy in strategies:
        if strategy == "installed" and installed_value is not None:
            result.append(installed_value)
        elif strategy == "latest" and ordered:
            result.append(ordered[-1])
        elif strategy == "explicit" and explicit:
            result.append(Version(explicit.removeprefix("v")))
        elif strategy == "adjacent" and installed_value is not None and ordered:
            lower = [item for item in ordered if item < installed_value]
            higher = [item for item in ordered if item > installed_value]
            if lower:
                result.append(lower[-1])
            if higher:
                result.append(higher[0])
        elif strategy not in {"installed", "latest", "explicit", "adjacent"}:
            raise ValueError(f"unsupported version strategy: {strategy}")
    if explicit:
        result.append(Version(explicit.removeprefix("v")))
    unique = sorted(set(result))
    return tuple(str(item) for item in unique)


def resolve_tag(library: CodeLibrary, version: str, available_tags: Sequence[str]) -> str:
    normalized = {tag.removeprefix("refs/tags/") for tag in available_tags}
    for pattern in library.tag_patterns:
        candidate = pattern.format(version=version)
        if candidate in normalized:
            return candidate
    for tag in normalized:
        parsed = version_from_tag(tag)
        if parsed is not None and str(parsed) == str(Version(version)):
            return tag
    raise ValueError(f"no official tag found for {library.name} {version}")
