"""Local POSIX identity backed RBAC for writing-material operations."""

from __future__ import annotations

import json
import os
import pwd
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from knowledgehub.core.atomic import atomic_write_json
from knowledgehub.core.hashing import sha256_json

RBAC_SCHEMA_VERSION = "writing-material-rbac-v1"
RBAC_PERMISSIONS = frozenset(
    {
        "writing_material.read",
        "writing_material.review",
        "writing_material.extract",
        "writing_material.index",
        "writing_material.release",
        "writing_material.retention_dispose",
        "writing_material.administer",
    }
)
RBAC_ROLES: Mapping[str, frozenset[str]] = {
    "viewer": frozenset({"writing_material.read"}),
    "reviewer": frozenset({"writing_material.read", "writing_material.review"}),
    "operator": frozenset(
        {
            "writing_material.read",
            "writing_material.extract",
            "writing_material.index",
        }
    ),
    "release_manager": frozenset(
        {
            "writing_material.read",
            "writing_material.index",
            "writing_material.release",
        }
    ),
    "retention_manager": frozenset(
        {"writing_material.read", "writing_material.retention_dispose"}
    ),
    "administrator": RBAC_PERMISSIONS,
}


class AccessPolicyError(ValueError):
    """The RBAC policy is missing, unsafe, malformed, or stale."""


class AccessDeniedError(PermissionError):
    """The authenticated local subject lacks a required permission."""


@dataclass(frozen=True, slots=True)
class PosixIdentity:
    subject: str
    uid: int


def current_posix_identity() -> PosixIdentity:
    uid = os.geteuid()
    return PosixIdentity(subject=pwd.getpwuid(uid).pw_name, uid=uid)


class WritingMaterialAccessControl:
    """Validate and enforce a private RBAC policy against the effective POSIX user."""

    def __init__(
        self,
        policy_path: Path,
        *,
        identity_provider: Callable[[], PosixIdentity] = current_posix_identity,
    ) -> None:
        # Keep the lexical path so a symlink at either the policy or access-dir
        # boundary remains observable and can be rejected by the safety checks.
        self.policy_path = Path(os.path.abspath(policy_path.expanduser()))
        self._identity_provider = identity_provider

    def bootstrap(
        self,
        *,
        subject: str,
        roles: Sequence[str],
        confirmed: bool,
    ) -> dict[str, Any]:
        if not confirmed:
            raise AccessPolicyError("RBAC bootstrap requires explicit confirmation")
        if self.policy_path.exists() or self.policy_path.is_symlink():
            raise AccessPolicyError("RBAC policy already exists and cannot be overwritten")
        identity = self._identity_provider()
        normalized_subject = subject.strip()
        if not normalized_subject or normalized_subject != identity.subject:
            raise AccessPolicyError("RBAC bootstrap subject must match the effective POSIX user")
        normalized_roles = sorted(set(roles))
        if not normalized_roles or any(role not in RBAC_ROLES for role in normalized_roles):
            raise AccessPolicyError("RBAC bootstrap contains an unknown or empty role set")
        access_dir = self.policy_path.parent
        if access_dir.is_symlink():
            raise AccessPolicyError("RBAC policy directory must not be a symlink")
        access_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        access_dir.chmod(0o700)
        now = datetime.now(timezone.utc).isoformat()
        payload: dict[str, Any] = {
            "schema_version": RBAC_SCHEMA_VERSION,
            "issued_at": now,
            "issuer": {
                "kind": "posix_euid",
                "subject": identity.subject,
                "uid": identity.uid,
            },
            "subjects": [
                {
                    "subject": normalized_subject,
                    "uid": identity.uid,
                    "roles": normalized_roles,
                }
            ],
        }
        policy = {**payload, "artifact_fingerprint": sha256_json(payload)}
        atomic_write_json(self.policy_path, policy, mode=0o600)
        return self.status()

    def status(self) -> dict[str, Any]:
        policy = self._load()
        identity = self._identity_provider()
        assignment = self._assignment(policy, identity)
        roles = list(assignment.get("roles", [])) if assignment is not None else []
        permissions = sorted(
            {permission for role in roles for permission in RBAC_ROLES[str(role)]}
        )
        return {
            "schema_version": "writing-material-rbac-status-v1",
            "status": "active",
            "policy_path": str(self.policy_path),
            "policy_fingerprint": policy["artifact_fingerprint"],
            "identity": {"kind": "posix_euid", "subject": identity.subject, "uid": identity.uid},
            "assigned": assignment is not None,
            "roles": roles,
            "permissions": permissions,
            "identity_enforced": True,
        }

    def check(self, permission: str) -> dict[str, Any]:
        if permission not in RBAC_PERMISSIONS:
            raise AccessPolicyError(f"unknown RBAC permission: {permission}")
        status = self.status()
        granted = permission in status["permissions"]
        return {
            **status,
            "status": "granted" if granted else "denied",
            "required_permission": permission,
            "granted": granted,
        }

    def require(self, permission: str) -> dict[str, Any]:
        decision = self.check(permission)
        if not decision["granted"]:
            identity = decision["identity"]
            raise AccessDeniedError(
                f"POSIX subject {identity['subject']} ({identity['uid']}) lacks {permission}"
            )
        return decision

    def _load(self) -> dict[str, Any]:
        path = self.policy_path
        if not path.is_file() or path.is_symlink():
            raise AccessPolicyError(f"RBAC policy is missing or unsafe: {path}")
        identity = self._identity_provider()
        for checked in (path.parent, path):
            stat = checked.stat()
            if stat.st_uid != identity.uid:
                raise AccessPolicyError(f"RBAC path owner differs from effective UID: {checked}")
            if stat.st_mode & 0o077:
                raise AccessPolicyError(f"RBAC path is accessible by group or other users: {checked}")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AccessPolicyError(f"RBAC policy is unreadable: {exc}") from exc
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "issued_at",
            "issuer",
            "subjects",
            "artifact_fingerprint",
        }:
            raise AccessPolicyError("RBAC policy has an invalid closed schema")
        if value.get("schema_version") != RBAC_SCHEMA_VERSION:
            raise AccessPolicyError("RBAC policy schema version is unsupported")
        issued_at = value.get("issued_at")
        try:
            parsed_at = datetime.fromisoformat(str(issued_at))
        except ValueError as exc:
            raise AccessPolicyError("RBAC policy issued_at is invalid") from exc
        if parsed_at.tzinfo is None:
            raise AccessPolicyError("RBAC policy issued_at must include a timezone")
        issuer = value.get("issuer")
        if (
            not isinstance(issuer, dict)
            or set(issuer) != {"kind", "subject", "uid"}
            or issuer.get("kind") != "posix_euid"
            or not isinstance(issuer.get("subject"), str)
            or not issuer["subject"].strip()
            or not isinstance(issuer.get("uid"), int)
            or issuer["uid"] < 0
        ):
            raise AccessPolicyError("RBAC policy issuer is invalid")
        subjects = value.get("subjects")
        if not isinstance(subjects, list) or not subjects:
            raise AccessPolicyError("RBAC policy subjects are invalid")
        seen: set[tuple[str, int]] = set()
        for assignment in subjects:
            if not isinstance(assignment, dict) or set(assignment) != {
                "subject",
                "uid",
                "roles",
            }:
                raise AccessPolicyError("RBAC subject assignment has an invalid closed schema")
            subject = assignment.get("subject")
            uid = assignment.get("uid")
            roles = assignment.get("roles")
            if (
                not isinstance(subject, str)
                or not subject.strip()
                or subject != subject.strip()
                or not isinstance(uid, int)
                or uid < 0
                or not isinstance(roles, list)
                or not roles
                or roles != sorted(set(roles))
                or any(not isinstance(role, str) or role not in RBAC_ROLES for role in roles)
            ):
                raise AccessPolicyError("RBAC subject assignment is invalid")
            key = (subject, uid)
            if key in seen:
                raise AccessPolicyError("RBAC policy contains a duplicate subject assignment")
            seen.add(key)
        payload = {key: value[key] for key in value if key != "artifact_fingerprint"}
        if value.get("artifact_fingerprint") != sha256_json(payload):
            raise AccessPolicyError("RBAC policy fingerprint is invalid")
        return value

    @staticmethod
    def _assignment(
        policy: Mapping[str, Any], identity: PosixIdentity
    ) -> Mapping[str, Any] | None:
        subjects = policy.get("subjects")
        assert isinstance(subjects, list)
        for assignment in subjects:
            assert isinstance(assignment, Mapping)
            if assignment.get("subject") == identity.subject and assignment.get("uid") == identity.uid:
                return assignment
        return None
