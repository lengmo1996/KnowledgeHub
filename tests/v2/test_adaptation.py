from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from knowledgehub.workflows.adaptation import AdaptationWorkflow, parse_debug_log


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "demo"
    root.mkdir()
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "train.py").write_text("Trainer(gpus=1)\n", encoding="utf-8")
    subprocess.run(["git", "add", "train.py"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=root, check=True, capture_output=True)
    return root


def test_evidence_change_verification_and_log_are_traceable(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    workflow = AdaptationWorkflow(root, tmp_path / "reports")
    evidence = workflow.create_evidence(
        issue="Trainer rejects gpus",
        environment={"name": "test", "python_version": "3.12", "packages": {"lightning": "2"}},
        affected_files=["train.py"],
        retrieved_evidence=[
            {
                "source_type": "source_code",
                "library": "lightning",
                "version": "2",
                "content": "Trainer(accelerator='gpu', devices=1)",
                "source_url": "https://example.test/source",
            }
        ],
        recommended_strategy="replace the removed argument",
        confidence=0.9,
    )
    (root / "train.py").write_text(
        "Trainer(accelerator='gpu', devices=1)\n", encoding="utf-8"
    )
    change = workflow.record_change(
        affected_files=["train.py"],
        reason="Lightning 2 removed gpus",
        old_api="Trainer(gpus=1)",
        new_api="Trainer(accelerator='gpu', devices=1)",
        evidence_ids=[evidence["evidence_id"]],
    )
    assert change["affected_files"][0]["before_sha256"]
    assert "accelerator" in Path(change["patch"]).read_text(encoding="utf-8")
    repeated_change = workflow.record_change(
        affected_files=["train.py"],
        reason="Lightning 2 removed gpus",
        old_api="Trainer(gpus=1)",
        new_api="Trainer(accelerator='gpu', devices=1)",
        evidence_ids=[evidence["evidence_id"]],
    )
    assert repeated_change["change_id"] == change["change_id"]
    verification = workflow.record_verification(
        name="compile",
        command="python -m py_compile train.py",
        exit_code=0,
        output="api_key=secret all good",
    )
    assert "secret" not in verification["output_excerpt"]
    repeated_verification = workflow.record_verification(
        name="compile",
        command="python -m py_compile train.py",
        exit_code=0,
        output="api_key=secret all good",
    )
    assert repeated_verification["verification_id"] == verification["verification_id"]
    with pytest.raises(ValueError, match="cannot be replaced"):
        workflow.create_evidence(
            issue="Trainer rejects gpus",
            environment={"name": "test"},
            affected_files=["train.py"],
            retrieved_evidence=[],
            recommended_strategy="replace",
            confidence=0.5,
        )
    result = workflow.finalize(unresolved_risks=["full training was not run"])
    assert result["status"] == "completed"
    report = Path(result["report"])
    assert report.is_file() and "full training was not run" in report.read_text(encoding="utf-8")
    audit = workflow.validate()
    assert audit["valid"] is True
    assert audit["checked"] == {
        "evidence_packages": 1,
        "changes": 1,
        "verifications": 1,
    }

    (root / "train.py").write_text("Trainer(gpus=8)\n", encoding="utf-8")
    invalid = workflow.validate()
    assert invalid["valid"] is False
    assert any("after hash mismatch" in error for error in invalid["errors"])
    assert any("patch differs" in error for error in invalid["errors"])


def test_adaptation_requires_evidence_and_contained_files(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    workflow = AdaptationWorkflow(root, tmp_path / "reports")
    with pytest.raises(ValueError, match="evidence package"):
        workflow.record_verification(name="x", command="x", exit_code=0, output="")
    outside = tmp_path / "outside.py"
    outside.write_text("pass\n", encoding="utf-8")
    with pytest.raises(ValueError, match="outside"):
        workflow.create_evidence(
            issue="x",
            environment={"name": "test"},
            affected_files=["../outside.py"],
            retrieved_evidence=[],
            recommended_strategy="inspect",
            confidence=0.1,
        )


def test_debug_log_separates_project_and_dependency_frames(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    log = f'''Traceback (most recent call last):
  File "{root / 'train.py'}", line 1, in main
  File "/env/lib/python3.12/site-packages/lightning/trainer.py", line 10, in __init__
TypeError: Trainer.__init__() got an unexpected keyword argument 'gpus'
'''
    result = parse_debug_log(log, root)
    assert result["exception_type"] == "TypeError"
    assert result["project_frames"] == 1 and result["dependency_frames"] == 1
    assert result["extracted_keywords"] == ["gpus"]
    assert result["trusted_as_instruction"] is False
