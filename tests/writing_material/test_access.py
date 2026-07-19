from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from knowledgehub.cli.writing_material import (
    _required_permission,
    add_writing_material_parser,
    run_writing_material_command,
)
from knowledgehub.core.hashing import sha256_json
from knowledgehub.writing_rag.access import (
    AccessDeniedError,
    AccessPolicyError,
    PosixIdentity,
    WritingMaterialAccessControl,
)


def _control(tmp_path: Path, subject: str = "lengmo") -> WritingMaterialAccessControl:
    identity = PosixIdentity(subject=subject, uid=os.geteuid())
    return WritingMaterialAccessControl(
        tmp_path / "access" / "rbac-policy.json",
        identity_provider=lambda: identity,
    )


def test_rbac_bootstrap_is_private_fingerprinted_and_authorizes_roles(tmp_path) -> None:
    control = _control(tmp_path)
    status = control.bootstrap(
        subject="lengmo",
        roles=["reviewer", "release_manager"],
        confirmed=True,
    )
    assert status["status"] == "active"
    assert status["identity_enforced"] is True
    assert status["roles"] == ["release_manager", "reviewer"]
    assert control.policy_path.stat().st_mode & 0o777 == 0o600
    assert control.policy_path.parent.stat().st_mode & 0o777 == 0o700
    assert control.require("writing_material.review")["granted"] is True
    assert control.require("writing_material.release")["granted"] is True
    with pytest.raises(AccessDeniedError, match=r"lacks writing_material\.extract"):
        control.require("writing_material.extract")


def test_rbac_rejects_unassigned_posix_subject_and_uid_pair(tmp_path) -> None:
    control = _control(tmp_path)
    control.bootstrap(subject="lengmo", roles=["reviewer"], confirmed=True)
    other = WritingMaterialAccessControl(
        control.policy_path,
        identity_provider=lambda: PosixIdentity(subject="other-user", uid=os.geteuid()),
    )
    decision = other.check("writing_material.read")
    assert decision["status"] == "denied"
    assert decision["assigned"] is False
    with pytest.raises(AccessDeniedError, match="other-user"):
        other.require("writing_material.read")


def test_rbac_rejects_tamper_closed_schema_and_permission_drift(tmp_path) -> None:
    control = _control(tmp_path)
    control.bootstrap(subject="lengmo", roles=["reviewer"], confirmed=True)
    policy = json.loads(control.policy_path.read_text(encoding="utf-8"))
    policy["subjects"][0]["roles"] = ["administrator"]
    control.policy_path.write_text(json.dumps(policy), encoding="utf-8")
    control.policy_path.chmod(0o600)
    with pytest.raises(AccessPolicyError, match="fingerprint"):
        control.status()

    payload = {key: value for key, value in policy.items() if key != "artifact_fingerprint"}
    payload["unexpected"] = True
    policy = {**payload, "artifact_fingerprint": sha256_json(payload)}
    control.policy_path.write_text(json.dumps(policy), encoding="utf-8")
    control.policy_path.chmod(0o600)
    with pytest.raises(AccessPolicyError, match="closed schema"):
        control.status()

    del payload["unexpected"]
    policy = {**payload, "artifact_fingerprint": sha256_json(payload)}
    control.policy_path.write_text(json.dumps(policy), encoding="utf-8")
    control.policy_path.chmod(0o640)
    with pytest.raises(AccessPolicyError, match="group or other"):
        control.status()


def test_rbac_bootstrap_requires_matching_identity_confirmation_and_no_overwrite(tmp_path) -> None:
    control = _control(tmp_path)
    with pytest.raises(AccessPolicyError, match="explicit confirmation"):
        control.bootstrap(subject="lengmo", roles=["reviewer"], confirmed=False)
    with pytest.raises(AccessPolicyError, match="must match"):
        control.bootstrap(subject="claimed-user", roles=["reviewer"], confirmed=True)
    with pytest.raises(AccessPolicyError, match="unknown or empty"):
        control.bootstrap(subject="lengmo", roles=[], confirmed=True)
    control.bootstrap(subject="lengmo", roles=["reviewer"], confirmed=True)
    with pytest.raises(AccessPolicyError, match="cannot be overwritten"):
        control.bootstrap(subject="lengmo", roles=["administrator"], confirmed=True)


def test_rbac_rejects_symlinked_policy_boundary(tmp_path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    access_link = tmp_path / "access"
    access_link.symlink_to(target, target_is_directory=True)
    identity = PosixIdentity(subject="lengmo", uid=os.geteuid())
    control = WritingMaterialAccessControl(
        access_link / "rbac-policy.json",
        identity_provider=lambda: identity,
    )
    with pytest.raises(AccessPolicyError, match="directory must not be a symlink"):
        control.bootstrap(subject="lengmo", roles=["reviewer"], confirmed=True)


def test_writing_material_cli_maps_commands_to_least_privilege() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    add_writing_material_parser(commands)

    cases = (
        (["extract", "--selection", "selection.json"], "writing_material.extract"),
        (["review", "render", "--run-id", "run-1"], "writing_material.review"),
        (["validate", "--run-id", "run-1"], "writing_material.read"),
        (
            [
                "index",
                "--run-id",
                "run-1",
                "--accepted-only",
                "--candidate-collection",
                "candidate",
            ],
            "writing_material.index",
        ),
        (["release", "rollback", "--dry-run"], "writing_material.release"),
        (
            ["pilot", "render-quality-review", "--run-id", "run-1", "--audit-report", "a.json", "--reviewer", "lengmo", "--output-dir", "out"],
            "writing_material.review",
        ),
    )
    for tail, permission in cases:
        args = parser.parse_args(["writing-material", *tail])
        assert _required_permission(args) == permission

    access_args = parser.parse_args(
        [
            "writing-material",
            "access",
            "bootstrap",
            "--subject",
            "lengmo",
            "--role",
            "administrator",
            "--yes",
        ]
    )
    assert access_args.roles == ["administrator"]
    assert access_args.yes is True


def test_cli_fails_closed_when_configured_policy_is_missing(
    tmp_path, monkeypatch, capsys
) -> None:
    policy = tmp_path / "access" / "missing.json"
    monkeypatch.setattr(
        "knowledgehub.cli.writing_material.HubConfig.load",
        lambda _path: SimpleNamespace(
            writing_materials=SimpleNamespace(rbac_policy_path=policy)
        ),
    )
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    add_writing_material_parser(commands)
    args = parser.parse_args(["writing-material", "validate", "--run-id", "run-1"])
    args.hub_config = None
    assert run_writing_material_command(args) == 2
    assert "RBAC policy is missing or unsafe" in capsys.readouterr().out


def test_cli_denies_command_before_business_service_dispatch(tmp_path, monkeypatch, capsys) -> None:
    control = _control(tmp_path)
    control.bootstrap(subject="lengmo", roles=["reviewer"], confirmed=True)
    monkeypatch.setattr(
        "knowledgehub.cli.writing_material.HubConfig.load",
        lambda _path: SimpleNamespace(
            writing_materials=SimpleNamespace(rbac_policy_path=control.policy_path)
        ),
    )
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    add_writing_material_parser(commands)
    args = parser.parse_args(["writing-material", "release", "rollback", "--dry-run"])
    args.hub_config = None
    assert run_writing_material_command(args) == 2
    output = capsys.readouterr().out
    assert "AccessDeniedError" in output
    assert "lacks writing_material.release" in output
