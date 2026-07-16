"""Canonical package, tag, branch, nightly and commit version identities."""

from __future__ import annotations

import re
from dataclasses import dataclass

from packaging.version import InvalidVersion, Version

_COMMIT = re.compile(r"^[0-9a-f]{7,40}$", re.I)


@dataclass(frozen=True, slots=True)
class NormalizedVersion:
    raw: str
    normalized: str
    local_build: str | None
    release_type: str
    tag: str | None = None
    commit: str | None = None

    @classmethod
    def parse(cls, raw: str, *, tag: str | None = None, commit: str | None = None) -> "NormalizedVersion":
        value = raw.strip()
        if value in {"main", "master"}:
            return cls(raw, value, None, "branch", tag, commit)
        if value == "nightly":
            return cls(raw, value, None, "nightly", tag, commit)
        if _COMMIT.fullmatch(value):
            return cls(raw, value.lower(), None, "commit", tag, value.lower())
        candidate = value.removeprefix("v")
        try:
            parsed = Version(candidate)
        except InvalidVersion as exc:
            raise ValueError(f"unsupported version: {raw}") from exc
        release_type = "prerelease" if parsed.is_prerelease else "dev" if parsed.is_devrelease else "stable"
        normalized = ".".join(str(item) for item in parsed.release)
        return cls(raw, normalized, parsed.local, release_type, tag or f"v{normalized}", commit)

    def to_dict(self) -> dict[str, str | None]:
        return {
            "raw": self.raw,
            "normalized": self.normalized,
            "local_build": self.local_build,
            "release_type": self.release_type,
            "tag": self.tag,
            "commit": self.commit,
        }
