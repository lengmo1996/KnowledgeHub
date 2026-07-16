"""Evidence-first repository adaptation records without executing target code."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from knowledgehub.core.atomic import atomic_write_json, atomic_write_text
from knowledgehub.core.hashing import sha256_bytes, sha256_json
from knowledgehub.core.logging import redact_text


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_debug_log(log: str, repository: Path) -> dict[str, Any]:
    """Extract a bounded traceback incident without treating log text as instructions."""
    root = repository.resolve(strict=True)
    frames: list[dict[str, Any]] = []
    pattern = re.compile(r'^\s*File "([^"]+)", line (\d+), in (.+?)\s*$')
    for line in log.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        raw_path, line_number, function = match.groups()
        path = Path(raw_path)
        candidate = path if path.is_absolute() else root / path
        try:
            relative = candidate.resolve(strict=False).relative_to(root)
            origin = "project"
            display_path = relative.as_posix()
        except ValueError:
            origin = "dependency" if "site-packages" in candidate.parts else "external"
            display_path = str(path)
        frames.append(
            {
                "path": display_path,
                "line": int(line_number),
                "function": function,
                "origin": origin,
            }
        )
    exception_type = None
    message = None
    exception_pattern = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception)):\s*(.*)$")
    for line in reversed(log.splitlines()):
        match = exception_pattern.match(line.strip())
        if match:
            exception_type, message = match.groups()
            break
    keywords = sorted(
        set(
            re.findall(
                r"(?:unexpected keyword argument|has no attribute) ['\"]([^'\"]+)['\"]",
                message or "",
            )
        )
    )
    query_terms = [value for value in (exception_type, *keywords) if value]
    if frames:
        query_terms.append(frames[-1]["function"])
    return {
        "schema_name": "debug_incident",
        "schema_version": "2.0",
        "exception_type": exception_type,
        "message": redact_text(message or "")[:2_000],
        "frames": frames[-100:],
        "project_frames": sum(item["origin"] == "project" for item in frames),
        "dependency_frames": sum(item["origin"] == "dependency" for item in frames),
        "extracted_keywords": keywords,
        "query_terms": query_terms,
        "candidate_roots": [
            "project_configuration_or_call_site",
            "dependency_api_or_version_mismatch" if any(item["origin"] == "dependency" for item in frames) else "unclassified_runtime_failure",
        ],
        "content_origin": "user_supplied_log",
        "trusted_as_instruction": False,
    }


class AdaptationWorkflow:
    """Persist evidence, diffs and externally executed verification results."""

    def __init__(self, repository: Path, output_root: Path) -> None:
        self.repository = repository.resolve(strict=True)
        if not self.repository.is_dir():
            raise ValueError("repository must be a directory")
        remote = self._run_git(self.repository, "remote", "get-url", "origin")
        slug = None
        if remote and (remote.startswith("https://github.com/") or remote.startswith("git@github.com:")):
            slug = remote.removeprefix("https://github.com/").removeprefix(
                "git@github.com:"
            ).removesuffix(".git")
        self.repository_name = slug or self.repository.name
        self.destination = output_root / (
            slug.replace("/", "--") if slug else self.repository.name
        )

    def create_evidence(
        self,
        *,
        issue: str,
        environment: Mapping[str, Any],
        affected_files: Sequence[str],
        retrieved_evidence: Sequence[Mapping[str, Any]],
        recommended_strategy: str,
        confidence: float,
        inferences: Sequence[str] = (),
        warnings: Sequence[str] = (),
    ) -> dict[str, Any]:
        if not issue.strip():
            raise ValueError("issue must not be empty")
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be between zero and one")
        files = [self._source_snapshot(value) for value in affected_files]
        commit = self._git_text("rev-parse", "HEAD")
        evidence_id = sha256_json(
            {
                "repository": str(self.repository),
                "commit": commit,
                "issue": issue,
                "files": [item["path"] for item in files],
                "environment": environment.get("name"),
            }
        )[:20]
        path = self.destination / "evidence" / f"{evidence_id}.json"
        state = self._state()
        existing = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
        if existing and state and state.get("changes"):
            raise ValueError("evidence packages cannot be replaced after changes are recorded")
        if existing and existing.get("affected_files") != files:
            raise ValueError("existing evidence package does not match the current source snapshot")
        value = {
            "schema_name": "adaptation_evidence_package",
            "schema_version": "2.0",
            "evidence_id": evidence_id,
            "repository": self.repository_name,
            "repository_root": str(self.repository),
            "upstream_commit": commit,
            "issue": issue.strip(),
            "affected_files": files,
            "target_environment": self._environment_summary(environment),
            "retrieved_evidence": [self._bounded_evidence(item) for item in retrieved_evidence],
            "recommended_strategy": recommended_strategy.strip(),
            "confidence": confidence,
            "inferences": [str(item) for item in inferences],
            "warnings": [str(item) for item in warnings],
            "created_at": existing.get("created_at") if existing else _now(),
        }
        atomic_write_json(path, value)
        if not state:
            state = {
                "schema_name": "adaptation_session",
                "schema_version": "2.0",
                "repository": self.repository_name,
                "repository_root": str(self.repository),
                "upstream_commit": commit,
                "status": "running",
                "evidence_packages": [],
                "changes": [],
                "verifications": [],
                "created_at": _now(),
            }
        if evidence_id not in state["evidence_packages"]:
            state["evidence_packages"].append(evidence_id)
        self._save(state)
        return {**value, "path": str(path)}

    def record_change(
        self,
        *,
        affected_files: Sequence[str],
        reason: str,
        old_api: str | None,
        new_api: str | None,
        evidence_ids: Sequence[str],
    ) -> dict[str, Any]:
        state = self._required_state()
        files = [self._relative_path(value) for value in affected_files]
        missing = sorted(set(evidence_ids) - set(state["evidence_packages"]))
        if missing:
            raise ValueError(f"unknown evidence package: {missing[0]}")
        file_changes = [self._file_change(path) for path in files]
        patch = self._git_text("diff", "--no-ext-diff", "--", *files, allow_empty=True)
        change_id = sha256_json(
            {
                "files": file_changes,
                "reason": reason,
                "old_api": old_api,
                "new_api": new_api,
                "evidence": list(evidence_ids),
            }
        )[:20]
        for existing in state["changes"]:
            if existing["change_id"] == change_id:
                return dict(existing)
        patch_path = self.destination / "patches" / f"{change_id}.diff"
        atomic_write_text(patch_path, patch[:200_000])
        value = {
            "change_id": change_id,
            "affected_files": file_changes,
            "reason": reason.strip(),
            "old_api": old_api,
            "new_api": new_api,
            "evidence_ids": list(evidence_ids),
            "patch": str(patch_path),
            "recorded_at": _now(),
        }
        state["changes"].append(value)
        self._save(state)
        return value

    def record_verification(
        self,
        *,
        name: str,
        command: str,
        exit_code: int,
        output: str,
        scope: str = "bounded",
    ) -> dict[str, Any]:
        state = self._required_state()
        sanitized = redact_text(output)[:20_000]
        verification_id = sha256_json(
            {
                "name": name,
                "command": command,
                "exit_code": exit_code,
                "output_sha256": sha256_bytes(output.encode("utf-8", errors="replace")),
            }
        )[:20]
        for existing in state["verifications"]:
            if existing["verification_id"] == verification_id:
                return dict(existing)
        value = {
            "verification_id": verification_id,
            "name": name.strip(),
            "command": redact_text(command),
            "exit_code": int(exit_code),
            "status": "passed" if exit_code == 0 else "failed",
            "scope": scope,
            "output_excerpt": sanitized,
            "output_sha256": sha256_bytes(output.encode("utf-8", errors="replace")),
            "recorded_at": _now(),
        }
        state["verifications"].append(value)
        self._save(state)
        return value

    def finalize(self, *, unresolved_risks: Sequence[str] = ()) -> dict[str, Any]:
        state = self._required_state()
        if not state["evidence_packages"] or not state["changes"] or not state["verifications"]:
            raise ValueError("finalization requires evidence, at least one change and verification")
        state["status"] = (
            "completed"
            if all(item["status"] == "passed" for item in state["verifications"])
            else "partial"
        )
        state["unresolved_risks"] = [str(item) for item in unresolved_risks]
        state["completed_at"] = _now()
        self._save(state)
        report = self.destination / "adaptation_log.md"
        atomic_write_text(report, self._markdown(state))
        return {**state, "report": str(report)}

    def validate(self) -> dict[str, Any]:
        """Audit a recorded adaptation against its pinned Git worktree."""
        state = self._required_state()
        errors: list[str] = []
        commit = self._git_text("rev-parse", "HEAD")
        if state.get("schema_name") != "adaptation_session":
            errors.append("invalid adaptation session schema_name")
        if state.get("schema_version") != "2.0":
            errors.append("invalid adaptation session schema_version")
        if state.get("repository_root") != str(self.repository):
            errors.append("repository_root differs from the inspected worktree")
        if not commit or state.get("upstream_commit") != commit:
            errors.append("upstream_commit differs from the inspected worktree")

        evidence_ids = [str(value) for value in state.get("evidence_packages") or []]
        if len(evidence_ids) != len(set(evidence_ids)):
            errors.append("duplicate evidence package IDs")
        for evidence_id in evidence_ids:
            self._validate_evidence(evidence_id, commit or "", errors)

        changes = state.get("changes") or []
        for change in changes:
            self._validate_change(change, set(evidence_ids), errors)

        verifications = state.get("verifications") or []
        for verification in verifications:
            if not isinstance(verification, Mapping):
                errors.append("verification record must be an object")
                continue
            expected_status = "passed" if verification.get("exit_code") == 0 else "failed"
            if verification.get("status") != expected_status:
                errors.append(
                    f"verification status/exit mismatch: {verification.get('verification_id')}"
                )
            if len(str(verification.get("output_sha256") or "")) != 64:
                errors.append(
                    f"verification output hash is invalid: {verification.get('verification_id')}"
                )
        if state.get("status") == "completed":
            if not evidence_ids or not changes or not verifications:
                errors.append("completed session lacks evidence, changes, or verification")
            if any(item.get("status") != "passed" for item in verifications):
                errors.append("completed session contains a failed verification")
            if not (self.destination / "adaptation_log.md").is_file():
                errors.append("completed session lacks adaptation_log.md")
        return {
            "check": "adaptation",
            "valid": not errors,
            "repository": state.get("repository"),
            "upstream_commit": state.get("upstream_commit"),
            "checked": {
                "evidence_packages": len(evidence_ids),
                "changes": len(changes),
                "verifications": len(verifications),
            },
            "errors": errors[:200],
        }

    def _validate_evidence(
        self, evidence_id: str, commit: str, errors: list[str]
    ) -> None:
        path = self.destination / "evidence" / f"{evidence_id}.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            errors.append(f"missing or invalid evidence package: {evidence_id}")
            return
        if not isinstance(value, Mapping):
            errors.append(f"evidence package must be an object: {evidence_id}")
            return
        if value.get("evidence_id") != evidence_id:
            errors.append(f"evidence identity mismatch: {evidence_id}")
        if value.get("upstream_commit") != commit:
            errors.append(f"evidence commit mismatch: {evidence_id}")
        if value.get("repository_root") != str(self.repository):
            errors.append(f"evidence repository mismatch: {evidence_id}")
        for item in value.get("affected_files") or []:
            if not isinstance(item, Mapping):
                errors.append(f"invalid affected file evidence: {evidence_id}")
                continue
            relative = str(item.get("path") or "")
            before = self._git_bytes("show", f"HEAD:{relative}") if relative else None
            if before is None or sha256_bytes(before) != item.get("content_sha256"):
                errors.append(f"evidence source hash mismatch: {evidence_id}:{relative}")

    def _validate_change(
        self,
        change: object,
        evidence_ids: set[str],
        errors: list[str],
    ) -> None:
        if not isinstance(change, Mapping):
            errors.append("change record must be an object")
            return
        change_id = str(change.get("change_id") or "")
        references = {str(value) for value in change.get("evidence_ids") or []}
        missing = references - evidence_ids
        if missing:
            errors.append(f"change {change_id} references unknown evidence: {sorted(missing)[0]}")
        files: list[str] = []
        for item in change.get("affected_files") or []:
            if not isinstance(item, Mapping):
                errors.append(f"change {change_id} has an invalid affected file")
                continue
            relative = str(item.get("path") or "")
            files.append(relative)
            path = self.repository / relative
            before = self._git_bytes("show", f"HEAD:{relative}")
            try:
                after = path.read_bytes()
            except OSError:
                errors.append(f"change {change_id} affected file is missing: {relative}")
                continue
            if (sha256_bytes(before) if before is not None else None) != item.get(
                "before_sha256"
            ):
                errors.append(f"change {change_id} before hash mismatch: {relative}")
            if sha256_bytes(after) != item.get("after_sha256"):
                errors.append(f"change {change_id} after hash mismatch: {relative}")
        patch = Path(str(change.get("patch") or ""))
        if patch.parent != self.destination / "patches" or patch.name != f"{change_id}.diff":
            errors.append(f"change {change_id} patch path is outside its session")
        else:
            current = self._git_text(
                "diff", "--no-ext-diff", "--", *files, allow_empty=True
            )
            try:
                recorded = patch.read_text(encoding="utf-8")
            except OSError:
                errors.append(f"change {change_id} patch is missing")
            else:
                if recorded != current[:200_000]:
                    errors.append(f"change {change_id} patch differs from the worktree")

    def _source_snapshot(self, value: str) -> dict[str, Any]:
        relative = self._relative_path(value)
        path = self.repository / relative
        content = path.read_bytes()
        text = content.decode("utf-8", errors="replace")
        return {
            "path": relative,
            "content_sha256": sha256_bytes(content),
            "content_excerpt": text[:4_000],
            "excerpt_truncated": len(text) > 4_000,
        }

    def _file_change(self, relative: str) -> dict[str, Any]:
        path = self.repository / relative
        after = path.read_bytes()
        before = self._git_bytes("show", f"HEAD:{relative}")
        return {
            "path": relative,
            "before_sha256": sha256_bytes(before) if before is not None else None,
            "after_sha256": sha256_bytes(after),
            "new_file": before is None,
        }

    def _relative_path(self, value: str) -> str:
        candidate = (self.repository / value).resolve(strict=True)
        try:
            relative = candidate.relative_to(self.repository)
        except ValueError as exc:
            raise ValueError("affected file is outside repository") from exc
        if not candidate.is_file():
            raise ValueError("affected path must be a file")
        return relative.as_posix()

    def _state(self) -> dict[str, Any] | None:
        path = self.destination / "adaptation.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def _required_state(self) -> dict[str, Any]:
        state = self._state()
        if state is None:
            raise ValueError("create an evidence package before recording adaptation work")
        return state

    def _save(self, state: dict[str, Any]) -> None:
        state["updated_at"] = _now()
        atomic_write_json(self.destination / "adaptation.json", state)

    def _git_text(self, *args: str, allow_empty: bool = False) -> str:
        value = self._run_git(self.repository, *args, preserve_output=True)
        if value is None and not allow_empty:
            raise RuntimeError("repository Git metadata is unavailable")
        return (value or "").strip() if args[:2] == ("rev-parse", "HEAD") else (value or "")

    def _git_bytes(self, *args: str) -> bytes | None:
        result = subprocess.run(
            ["git", *args],
            cwd=self.repository,
            check=False,
            capture_output=True,
            timeout=30,
        )
        return result.stdout if result.returncode == 0 else None

    @staticmethod
    def _run_git(root: Path, *args: str, preserve_output: bool = False) -> str | None:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode:
            return None
        return result.stdout if preserve_output else result.stdout.strip()

    @staticmethod
    def _environment_summary(environment: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: environment.get(key)
            for key in ("name", "python_version", "platform", "cuda", "gpus", "packages", "captured_at")
        }

    @staticmethod
    def _bounded_evidence(item: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {
            "source_type",
            "library",
            "version",
            "symbol",
            "content",
            "source_url",
            "commit",
            "evidence_role",
            "inference",
        }
        value = {key: item.get(key) for key in allowed if item.get(key) is not None}
        if "content" in value:
            value["content"] = str(value["content"])[:4_000]
        value.setdefault("evidence_role", "retrieved_evidence")
        value.setdefault("inference", False)
        return value

    @staticmethod
    def _markdown(state: Mapping[str, Any]) -> str:
        lines = [
            f"# Adaptation log: {state['repository']}",
            "",
            f"- Upstream commit: `{state['upstream_commit']}`",
            f"- Status: `{state['status']}`",
            f"- Evidence packages: {len(state['evidence_packages'])}",
            "",
            "## Changes",
            "",
        ]
        for item in state["changes"]:
            files = ", ".join(value["path"] for value in item["affected_files"])
            lines.extend(
                [
                    f"### `{item['change_id']}`",
                    "",
                    f"- Files: {files}",
                    f"- Reason: {item['reason']}",
                    f"- API: `{item.get('old_api') or 'n/a'}` → `{item.get('new_api') or 'n/a'}`",
                    f"- Evidence: {', '.join(item['evidence_ids'])}",
                    "",
                ]
            )
        lines.extend(["## Verification", ""])
        for item in state["verifications"]:
            lines.append(
                f"- **{item['status']}** `{item['name']}` — `{item['command']}` ({item['scope']})"
            )
        lines.extend(["", "## Unresolved risks", ""])
        risks = state.get("unresolved_risks") or []
        lines.extend([f"- {item}" for item in risks] or ["- None recorded."])
        lines.extend(
            [
                "",
                "## Evidence boundary",
                "",
                "This log records bounded static/runtime checks. It does not imply full training, dataset, GPU, or dependency-matrix validation.",
                "",
            ]
        )
        return "\n".join(lines)
