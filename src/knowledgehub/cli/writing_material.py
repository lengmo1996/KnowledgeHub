"""CLI for review-gated Zotero writing-material extraction."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Mapping

from knowledgehub.core.atomic import atomic_write_json
from knowledgehub.core.hashing import sha256_text
from knowledgehub.governance.tasks import TaskExecutor, TaskStore, default_task_store_path
from knowledgehub.hub.config import HubConfig
from knowledgehub.indexing.incremental import IncrementalChunkIndexer
from knowledgehub.writing_rag.access import (
    RBAC_PERMISSIONS,
    RBAC_ROLES,
    WritingMaterialAccessControl,
)
from knowledgehub.writing_rag.extract import WritingMaterialExtractionService
from knowledgehub.writing_rag.materials import infer_writing_asset_type
from knowledgehub.writing_rag.pilot import (
    AcceptedCorpusQualityAuditor,
    AcceptedCorpusQualityReviewRenderer,
    CandidateRetrievalEvaluator,
    ControlledPilotEvaluator,
    PilotRetrievalOutcome,
    create_pilot_approval,
    provider_preflight,
)
from knowledgehub.writing_rag.release import QdrantReleaseBackend, WritingMaterialReleaseService
from knowledgehub.writing_rag.release_retention import (
    WritingMaterialReleaseRetirementService,
)
from knowledgehub.writing_rag.retention import WritingMaterialRetentionService
from knowledgehub.writing_rag.retention_coordinator import (
    WritingMaterialRetentionCoordinator,
)
from knowledgehub.writing_rag.review import (
    WritingMaterialCandidateIndexer,
    WritingMaterialReviewService,
)


def add_writing_material_parser(subparsers: Any) -> None:
    root = subparsers.add_parser(
        "writing-material",
        help="Extract and review provenance-verified writing materials",
    )
    commands = root.add_subparsers(dest="writing_material_command", required=True)

    access = commands.add_parser("access")
    access_commands = access.add_subparsers(dest="writing_material_access_command", required=True)
    bootstrap_access = access_commands.add_parser("bootstrap")
    bootstrap_access.add_argument("--subject", required=True)
    bootstrap_access.add_argument(
        "--role", action="append", dest="roles", choices=sorted(RBAC_ROLES), required=True
    )
    bootstrap_access.add_argument("--yes", action="store_true")
    access_commands.add_parser("status")
    check_access = access_commands.add_parser("check")
    check_access.add_argument("--permission", choices=sorted(RBAC_PERMISSIONS), required=True)

    retention = commands.add_parser("retention")
    retention_commands = retention.add_subparsers(
        dest="writing_material_retention_command", required=True
    )
    retention_plan = retention_commands.add_parser("plan")
    retention_plan.add_argument("--run-id")
    retention_plan.add_argument("--output", type=Path)
    cache_scope_plan = retention_commands.add_parser("plan-cache-scope")
    cache_scope_plan.add_argument("--run-id", required=True)
    cache_scope_plan.add_argument("--output", type=Path)
    retention_quarantine = retention_commands.add_parser("quarantine")
    retention_quarantine.add_argument("--run-id", required=True)
    retention_quarantine.add_argument("--yes", action="store_true")
    retention_purge = retention_commands.add_parser("purge")
    retention_purge.add_argument("--run-id", required=True)
    retention_purge.add_argument("--yes", action="store_true")
    migrate_cache_scope = retention_commands.add_parser("migrate-cache-scope")
    migrate_cache_scope.add_argument("--run-id", required=True)
    migrate_cache_scope.add_argument("--yes", action="store_true")
    purge_cache_scope = retention_commands.add_parser("purge-cache-scope")
    purge_cache_scope.add_argument("--run-id", required=True)
    purge_cache_scope.add_argument("--yes", action="store_true")
    release_retirement_plan = retention_commands.add_parser("plan-release-retirement")
    release_retirement_plan.add_argument("--run-id", required=True)
    release_retirement_plan.add_argument("--output", type=Path)
    decommission_release = retention_commands.add_parser("decommission-release")
    decommission_release.add_argument("--run-id", required=True)
    decommission_release.add_argument("--yes", action="store_true")
    reference_purge_plan = retention_commands.add_parser("plan-reference-purge")
    reference_purge_plan.add_argument("--run-id", required=True)
    reference_purge_plan.add_argument("--output", type=Path)
    purge_references = retention_commands.add_parser("purge-references")
    purge_references.add_argument("--run-id", required=True)
    purge_references.add_argument("--yes", action="store_true")
    disposition_plan = retention_commands.add_parser("plan-disposition")
    disposition_plan.add_argument("--run-id", required=True)
    disposition_plan.add_argument("--output", type=Path)
    disposition = retention_commands.add_parser("dispose")
    disposition.add_argument("--run-id", required=True)
    disposition.add_argument("--yes", action="store_true")
    disposition_purge_plan = retention_commands.add_parser("plan-disposition-purge")
    disposition_purge_plan.add_argument("--run-id", required=True)
    disposition_purge_plan.add_argument("--output", type=Path)
    disposition_purge = retention_commands.add_parser("purge-disposition")
    disposition_purge.add_argument("--run-id", required=True)
    disposition_purge.add_argument("--yes", action="store_true")

    extract = commands.add_parser("extract")
    extract.add_argument("--selection", type=Path)
    extract.add_argument("--document-id", action="append", dest="document_ids", default=[])
    extract.add_argument("--collection", action="append", dest="collections", default=[])
    extract.add_argument(
        "--section",
        action="append",
        dest="sections",
        choices=("introduction", "results", "discussion", "conclusion"),
        default=[],
    )
    extract.add_argument("--limit", type=int)
    extract.add_argument("--retry-failed", action="store_true")
    extract.add_argument(
        "--run-id",
        help="With --retry-failed, reuse the prior run selection and create a new immutable run",
    )
    extract.add_argument("--resume-run-id", help="Resume one unfinished checkpointed run in place")
    extract.add_argument(
        "--pilot-approval",
        type=Path,
        help="Bind a non-dry-run extraction to an explicit pilot approval manifest",
    )
    extract.add_argument(
        "--output",
        type=Path,
        help="Explicit 0600 JSON output for dry-run only",
    )
    extract.add_argument("--dry-run", action="store_true")

    review = commands.add_parser("review")
    review_commands = review.add_subparsers(dest="writing_material_review_command", required=True)
    render = review_commands.add_parser("render")
    render.add_argument("--run-id", required=True)
    apply = review_commands.add_parser("apply")
    apply.add_argument("--run-id", required=True)
    apply.add_argument("--decisions", type=Path, required=True)
    apply.add_argument(
        "--allow-partial-snapshot",
        action="store_true",
        help="Write accepted-partial/ with explicit pending counts instead of requiring full review",
    )
    apply_quality = review_commands.add_parser("apply-quality")
    apply_quality.add_argument("--run-id", required=True)
    apply_quality.add_argument("--packet", type=Path, required=True)
    apply_quality.add_argument("--decisions", type=Path, required=True)
    apply_quality.add_argument("--dry-run", action="store_true")
    apply_quality.add_argument("--yes", action="store_true")
    reconcile_quality = review_commands.add_parser("reconcile-quality-receipt")
    reconcile_quality.add_argument("--run-id", required=True)
    reconcile_quality.add_argument("--packet", type=Path, required=True)
    reconcile_quality.add_argument("--decisions", type=Path, required=True)
    reconcile_quality.add_argument("--dry-run", action="store_true")
    reconcile_quality.add_argument("--yes", action="store_true")

    validate = commands.add_parser("validate")
    validate.add_argument("--run-id", required=True)
    validate.add_argument("--no-source-check", action="store_true")

    index = commands.add_parser("index")
    index.add_argument("--run-id", required=True)
    index.add_argument("--accepted-only", action="store_true", required=True)
    index.add_argument("--candidate-collection", required=True)
    index.add_argument("--dry-run", action="store_true")

    release = commands.add_parser("release")
    release_commands = release.add_subparsers(
        dest="writing_material_release_command", required=True
    )
    build = release_commands.add_parser("build")
    build.add_argument("--run-id", required=True)
    build.add_argument("--candidate-collection", required=True)
    build.add_argument("--dry-run", action="store_true")
    stage = release_commands.add_parser("stage")
    stage.add_argument("--manifest", type=Path, required=True)
    stage.add_argument("--yes", action="store_true")
    promote = release_commands.add_parser("promote")
    promote.add_argument("--yes", action="store_true")
    rollback = release_commands.add_parser("rollback")
    rollback.add_argument("--yes", action="store_true")
    rollback.add_argument("--dry-run", action="store_true")

    pilot = commands.add_parser("pilot")
    pilot_commands = pilot.add_subparsers(dest="writing_material_pilot_command", required=True)
    assess = pilot_commands.add_parser("assess-dry-run")
    assess.add_argument("--report", type=Path, required=True)
    assess.add_argument("--output", type=Path)
    preflight = pilot_commands.add_parser("preflight-provider")
    preflight.add_argument("--gate-report", type=Path, required=True)
    preflight.add_argument("--output", type=Path)
    approve = pilot_commands.add_parser("approve-extraction")
    approve.add_argument("--gate-report", type=Path, required=True)
    approve.add_argument("--output", type=Path, required=True)
    approve.add_argument("--approver", required=True)
    approve.add_argument("--reviewer", required=True)
    approve.add_argument("--rights-basis", required=True)
    approve.add_argument("--retention-policy", required=True)
    approve.add_argument("--access-policy", required=True)
    approve.add_argument("--yes", action="store_true")
    evaluate = pilot_commands.add_parser("evaluate")
    evaluate.add_argument("--run-id", required=True)
    evaluate.add_argument("--candidate-report", type=Path)
    evaluate.add_argument("--retrieval-report", type=Path)
    retrieval = pilot_commands.add_parser("evaluate-retrieval")
    retrieval.add_argument("--run-id", required=True)
    retrieval.add_argument("--candidate-report", type=Path, required=True)
    retrieval.add_argument("--queries", type=Path, required=True)
    retrieval.add_argument("--mode", choices=("sparse", "hybrid", "dense"), default="sparse")
    retrieval.add_argument("--output", type=Path)
    quality = pilot_commands.add_parser("audit-quality")
    quality.add_argument("--run-id", required=True)
    quality.add_argument("--output", type=Path)
    quality_review = pilot_commands.add_parser("render-quality-review")
    quality_review.add_argument("--run-id", required=True)
    quality_review.add_argument("--audit-report", type=Path, required=True)
    quality_review.add_argument("--reviewer", required=True)
    quality_review.add_argument("--output-dir", type=Path, required=True)


def run_writing_material_command(args: argparse.Namespace) -> int:
    try:
        config = HubConfig.load(args.hub_config or Path("configs/knowledgehub.yaml"))
        materials = config.writing_materials
        rbac_policy_path = getattr(materials, "rbac_policy_path", None)
        access = (
            WritingMaterialAccessControl(rbac_policy_path) if rbac_policy_path is not None else None
        )
        if args.writing_material_command == "access":
            if access is None:
                raise ValueError("writing-material RBAC policy path is not configured")
            if args.writing_material_access_command == "bootstrap":
                result = access.bootstrap(
                    subject=args.subject,
                    roles=args.roles,
                    confirmed=args.yes,
                )
            elif args.writing_material_access_command == "status":
                result = access.status()
            else:
                result = access.check(args.permission)
            _emit(result)
            return 0 if result.get("status") != "denied" else 1
        access_authorization = (
            access.require(_required_permission(args)) if access is not None else None
        )
        review = WritingMaterialReviewService(
            materials.data_root,
            materials.literature_data_dir,
            access_authorization=access_authorization,
        )
        if args.writing_material_command == "retention":
            retention = WritingMaterialRetentionService(materials.data_root)
            command = args.writing_material_retention_command
            if command in {
                "plan-release-retirement",
                "decommission-release",
                "plan-reference-purge",
                "purge-references",
                "plan-disposition",
                "dispose",
                "plan-disposition-purge",
                "purge-disposition",
            }:
                if access is not None:
                    access.require("writing_material.release")
                retirement = _release_retirement_service(config)
                coordinator = WritingMaterialRetentionCoordinator(retention, retirement)
                try:
                    if command == "plan-release-retirement":
                        result = retirement.plan(args.run_id)
                        if args.output is not None:
                            atomic_write_json(args.output, result, mode=0o600)
                    elif command == "plan-reference-purge":
                        result = retirement.reference_purge_plan(args.run_id)
                        if args.output is not None:
                            atomic_write_json(args.output, result, mode=0o600)
                    elif command == "plan-disposition":
                        result = coordinator.plan(args.run_id)
                        if args.output is not None:
                            atomic_write_json(args.output, result, mode=0o600)
                    elif command == "plan-disposition-purge":
                        result = coordinator.purge_plan(args.run_id)
                        if args.output is not None:
                            atomic_write_json(args.output, result, mode=0o600)
                    else:
                        if command == "decommission-release":

                            def operation() -> dict[str, Any]:
                                return retirement.decommission(args.run_id, confirmed=args.yes)

                            receipt_group = "release-retirement-receipts"
                        elif command == "purge-references":

                            def operation() -> dict[str, Any]:
                                return retirement.purge_references(args.run_id, confirmed=args.yes)

                            receipt_group = "release-reference-purge-receipts"
                        elif command == "dispose":

                            def operation() -> dict[str, Any]:
                                return coordinator.dispose(args.run_id, confirmed=args.yes)

                            receipt_group = "coordinated-receipts"
                        else:

                            def operation() -> dict[str, Any]:
                                return coordinator.purge(args.run_id, confirmed=args.yes)

                            receipt_group = "coordinated-purge-receipts"
                        result = _executor().execute(
                            f"writing_material_retention_{command}",
                            operation,
                            knowledge_base="writing",
                            version=args.run_id,
                            inputs={
                                "run_id": args.run_id,
                                "operation": command,
                                "confirmed": args.yes,
                            },
                            lock_keys=(
                                "derive:writing-materials",
                                "index:writing:promotion",
                                f"retention:writing-materials:{args.run_id}",
                            ),
                            output_manifest=lambda _value: str(
                                materials.data_root
                                / "retention"
                                / receipt_group
                                / f"{args.run_id}.json"
                            ),
                        )
                finally:
                    retirement.close()
            elif command in {"plan", "plan-cache-scope"}:
                result = (
                    retention.plan(args.run_id)
                    if command == "plan"
                    else retention.cache_scope_plan(args.run_id)
                )
                if args.output is not None:
                    atomic_write_json(args.output, result, mode=0o600)
            else:
                if command == "quarantine":

                    def operation() -> dict[str, Any]:
                        return retention.quarantine(args.run_id, confirmed=args.yes)

                    receipt_group = "receipts"
                elif command == "purge":

                    def operation() -> dict[str, Any]:
                        return retention.purge(args.run_id, confirmed=args.yes)

                    receipt_group = "receipts"
                elif command == "migrate-cache-scope":

                    def operation() -> dict[str, Any]:
                        return retention.migrate_legacy_cache_scope(
                            args.run_id,
                            confirmed=args.yes,
                        )

                    receipt_group = "cache-scope-receipts"
                else:

                    def operation() -> dict[str, Any]:
                        return retention.purge_cache_scope(
                            args.run_id,
                            confirmed=args.yes,
                        )

                    receipt_group = "cache-purge-receipts"
                result = _executor().execute(
                    f"writing_material_retention_{command}",
                    operation,
                    knowledge_base="writing",
                    version=args.run_id,
                    inputs={"run_id": args.run_id, "operation": command, "confirmed": args.yes},
                    lock_keys=(
                        "derive:writing-materials",
                        f"retention:writing-materials:{args.run_id}",
                    ),
                    output_manifest=lambda _value: str(
                        materials.data_root / "retention" / receipt_group / f"{args.run_id}.json"
                    ),
                )
            _emit(result)
            return 0 if result.get("status") not in {"blocked", "failed"} else 1
        if args.writing_material_command == "extract":
            selection = _selection_for_extract(args, review)
            pilot_approval = _read_object(args.pilot_approval) if args.pilot_approval else None
            service = WritingMaterialExtractionService(materials.runtime_config())
            try:

                def operation() -> dict[str, Any]:
                    return service.extract(
                        selection=selection,
                        document_ids=args.document_ids,
                        collections=args.collections,
                        sections=args.sections,
                        limit=args.limit,
                        dry_run=args.dry_run,
                        retry_failed=args.retry_failed,
                        resume_run_id=args.resume_run_id,
                        pilot_approval=pilot_approval,
                    )

                if args.dry_run:
                    result = operation()
                else:
                    service.validate_execution_authorization(
                        selection=selection,
                        document_ids=args.document_ids,
                        collections=args.collections,
                        sections=args.sections,
                        limit=args.limit,
                        resume_run_id=args.resume_run_id,
                        pilot_approval=pilot_approval,
                    )
                    result = _executor().execute(
                        "writing_material_extract",
                        operation,
                        knowledge_base="writing",
                        inputs={
                            "selection": str(selection.resolve())
                            if selection is not None
                            else None,
                            "selection_sha256": (
                                sha256_text(selection.read_text(encoding="utf-8"))
                                if selection is not None
                                else None
                            ),
                            "document_ids": args.document_ids,
                            "collections": args.collections,
                            "sections": args.sections,
                            "limit": args.limit,
                            "retry_failed": args.retry_failed,
                            "resume_run_id": args.resume_run_id,
                            "pilot_approval": (
                                str(args.pilot_approval.resolve())
                                if args.pilot_approval is not None
                                else None
                            ),
                            "pilot_approval_sha256": (
                                sha256_text(args.pilot_approval.read_text(encoding="utf-8"))
                                if args.pilot_approval is not None
                                else None
                            ),
                        },
                        input_manifest=(
                            str(selection.resolve())
                            if selection is not None
                            else str(review.run_dir(args.resume_run_id) / "manifest.json")
                            if args.resume_run_id
                            else None
                        ),
                        lock_keys=("derive:writing-materials",),
                        output_manifest=lambda value: str(
                            Path(str(value["run_dir"])) / "manifest.json"
                        ),
                    )
            finally:
                service.close()
            if args.output is not None:
                if not args.dry_run:
                    raise ValueError("--output is only valid with extraction --dry-run")
                atomic_write_json(args.output, result, mode=0o600)
            _emit(result)
            return 0
        if args.writing_material_command == "review":
            if args.writing_material_review_command == "render":

                def operation() -> dict[str, Any]:
                    return review.render(args.run_id)

                result = _executor().execute(
                    "writing_material_review",
                    operation,
                    knowledge_base="writing",
                    version=args.run_id,
                    inputs={"run_id": args.run_id, "operation": "render"},
                    lock_keys=(f"review:writing-materials:{args.run_id}",),
                    output_manifest=lambda value: str(value["review_report"]),
                )
            elif args.writing_material_review_command == "apply":

                def operation() -> dict[str, Any]:
                    return review.apply(
                        args.run_id,
                        args.decisions,
                        allow_partial_snapshot=args.allow_partial_snapshot,
                    )

                result = _executor().execute(
                    "writing_material_review",
                    operation,
                    knowledge_base="writing",
                    version=args.run_id,
                    inputs={
                        "run_id": args.run_id,
                        "operation": "apply",
                        "decisions": str(args.decisions.resolve()),
                        "decisions_sha256": sha256_text(args.decisions.read_text(encoding="utf-8")),
                        "allow_partial_snapshot": args.allow_partial_snapshot,
                    },
                    input_manifest=str(args.decisions.resolve()),
                    lock_keys=(f"review:writing-materials:{args.run_id}",),
                    output_manifest=lambda value: str(
                        value["accepted_snapshot"]["path"] + "/manifest.json"
                    ),
                )
            elif args.writing_material_review_command == "apply-quality":

                def operation() -> dict[str, Any]:
                    return review.apply_quality_review(
                        args.run_id,
                        packet_path=args.packet,
                        decisions_path=args.decisions,
                        dry_run=args.dry_run,
                        confirmed=args.yes,
                    )

                if args.dry_run:
                    result = operation()
                else:
                    result = _executor().execute(
                        "writing_material_quality_review",
                        operation,
                        knowledge_base="writing",
                        version=args.run_id,
                        inputs={
                            "run_id": args.run_id,
                            "operation": "apply-quality",
                            "packet": str(args.packet.resolve()),
                            "packet_sha256": sha256_text(args.packet.read_text(encoding="utf-8")),
                            "decisions": str(args.decisions.resolve()),
                            "decisions_sha256": sha256_text(
                                args.decisions.read_text(encoding="utf-8")
                            ),
                            "confirmed": args.yes,
                        },
                        input_manifest=str(args.decisions.resolve()),
                        lock_keys=(f"review:writing-materials:{args.run_id}",),
                        output_manifest=lambda value: str(
                            value["accepted_snapshot"]["path"] + "/manifest.json"
                        ),
                    )
            else:

                def operation() -> dict[str, Any]:
                    return review.reconcile_quality_review_receipt(
                        args.run_id,
                        packet_path=args.packet,
                        decisions_path=args.decisions,
                        dry_run=args.dry_run,
                        confirmed=args.yes,
                    )

                if args.dry_run:
                    result = operation()
                else:
                    result = _executor().execute(
                        "writing_material_quality_receipt_reconciliation",
                        operation,
                        knowledge_base="writing",
                        version=args.run_id,
                        inputs={
                            "run_id": args.run_id,
                            "operation": "reconcile-quality-receipt",
                            "packet": str(args.packet.resolve()),
                            "packet_sha256": sha256_text(args.packet.read_text(encoding="utf-8")),
                            "decisions": str(args.decisions.resolve()),
                            "decisions_sha256": sha256_text(
                                args.decisions.read_text(encoding="utf-8")
                            ),
                            "confirmed": args.yes,
                        },
                        input_manifest=str(args.decisions.resolve()),
                        lock_keys=(f"review:writing-materials:{args.run_id}",),
                        output_manifest=lambda value: str(value["quality_review_receipt"]["path"]),
                    )
            _emit(result)
            return 0
        if args.writing_material_command == "validate":
            result = review.validate(
                args.run_id,
                verify_source=not args.no_source_check,
            )
            _emit(result)
            return 0 if result["status"] == "success" else 1
        if args.writing_material_command == "release":
            result = _run_release_command(args, config, review)
            _emit(result)
            return 0
        if args.writing_material_command == "pilot":
            evaluator = ControlledPilotEvaluator(review)
            if args.writing_material_pilot_command == "assess-dry-run":
                result = evaluator.assess_dry_run(_read_object(args.report))
                if args.output is not None:
                    atomic_write_json(args.output, result, mode=0o600)
            elif args.writing_material_pilot_command == "preflight-provider":
                result = provider_preflight(
                    _read_object(args.gate_report), materials.runtime_config()
                )
                if args.output is not None:
                    atomic_write_json(args.output, result, mode=0o600)
            elif args.writing_material_pilot_command == "approve-extraction":
                runtime = materials.runtime_config()
                result = create_pilot_approval(
                    _read_object(args.gate_report),
                    output=args.output,
                    approver=args.approver,
                    reviewer=args.reviewer,
                    rights_basis=args.rights_basis,
                    retention_policy=args.retention_policy,
                    access_policy=args.access_policy,
                    provider=runtime.provider,
                    model=runtime.effective_model,
                    confirmed=args.yes,
                ) | {"manifest_path": str(args.output.resolve())}
            elif args.writing_material_pilot_command == "evaluate":
                result = evaluator.evaluate(
                    args.run_id,
                    candidate_report=(
                        _read_object(args.candidate_report) if args.candidate_report else None
                    ),
                    retrieval_report=(
                        _read_object(args.retrieval_report) if args.retrieval_report else None
                    ),
                )
            elif args.writing_material_pilot_command == "audit-quality":
                result = AcceptedCorpusQualityAuditor(review).audit(args.run_id)
                if args.output is not None:
                    atomic_write_json(args.output, result, mode=0o600)
            elif args.writing_material_pilot_command == "render-quality-review":
                packet = AcceptedCorpusQualityReviewRenderer(review).render(
                    args.run_id,
                    quality_report=_read_object(args.audit_report),
                    reviewer=args.reviewer,
                    output_dir=args.output_dir,
                )
                result = {
                    key: packet[key]
                    for key in (
                        "schema_name",
                        "schema_version",
                        "status",
                        "run_id",
                        "reviewer",
                        "quality_audit_fingerprint",
                        "artifact_fingerprint",
                        "counts",
                        "packet_path",
                        "markdown_path",
                        "decision_import_ready",
                        "requires_explicit_reviewer_decision",
                        "evidence_text_included",
                        "provenance_excerpt_included",
                        "review_decisions_modified",
                        "accepted_snapshot_modified",
                        "index_modified",
                        "llm_called",
                    )
                }
            else:
                result = _run_pilot_retrieval_command(args, config, review)
                if args.output is not None:
                    atomic_write_json(args.output, result, mode=0o600)
            _emit(result)
            return (
                0
                if result["status"]
                in {
                    "ready",
                    "success",
                    "approved_for_small_batch_extraction",
                    "eligible_for_manual_expansion_decision",
                }
                else 1
            )
        active = config.rag_config("writing").qdrant_collection
        candidate_root = (
            materials.data_root / "index-candidates" / sha256_text(args.candidate_collection)[:24]
        )
        indexer = WritingMaterialCandidateIndexer(review, config.rag_config("writing"))

        def operation() -> dict[str, Any]:
            return indexer.build(
                args.run_id,
                candidate_collection=args.candidate_collection,
                active_collection=active,
                candidate_data_dir=candidate_root,
                dry_run=args.dry_run,
            )

        if args.dry_run:
            result = operation()
        else:
            result = _executor().execute(
                "writing_material_index",
                operation,
                knowledge_base="writing",
                version=args.run_id,
                inputs={
                    "run_id": args.run_id,
                    "candidate_collection": args.candidate_collection,
                    "accepted_only": True,
                },
                input_manifest=str(review.accepted_dir(args.run_id) / "manifest.json"),
                lock_keys=(f"index:writing:{args.candidate_collection}",),
                output_manifest=lambda value: str(value["manifest_path"]),
            )
        _emit(result)
        return 0
    except Exception as exc:
        _emit({"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
        return 2


def _required_permission(args: argparse.Namespace) -> str:
    command = args.writing_material_command
    if command == "extract":
        return "writing_material.extract"
    if command == "review":
        return "writing_material.review"
    if command == "index":
        return "writing_material.index"
    if command == "release":
        return "writing_material.release"
    if command == "retention":
        return "writing_material.retention_dispose"
    if command == "pilot":
        pilot_command = args.writing_material_pilot_command
        if pilot_command == "approve-extraction":
            return "writing_material.extract"
        if pilot_command == "render-quality-review":
            return "writing_material.review"
    return "writing_material.read"


def _selection_for_extract(
    args: argparse.Namespace,
    review: WritingMaterialReviewService,
) -> Path | None:
    if args.output is not None and not args.dry_run:
        raise ValueError("--output is only valid with extraction --dry-run")
    if args.dry_run and args.pilot_approval is not None:
        raise ValueError("--pilot-approval is only valid for non-dry-run extraction")
    if args.resume_run_id:
        if (
            args.retry_failed
            or args.run_id
            or args.selection
            or args.document_ids
            or args.collections
            or args.pilot_approval
        ):
            raise ValueError("--resume-run-id cannot be combined with new or retry selectors")
        return None
    if args.retry_failed and args.run_id:
        manifest_path = review.run_dir(args.run_id) / "manifest.json"
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        selection = value.get("selection") if isinstance(value, Mapping) else None
        if not isinstance(selection, str) or not Path(selection).is_file():
            raise ValueError("prior run selection is unavailable")
        return Path(selection)
    if args.run_id:
        raise ValueError("--run-id is only valid with --retry-failed")
    if args.retry_failed:
        raise ValueError("--retry-failed requires --run-id")
    if args.selection is None and not args.document_ids and not args.collections:
        raise ValueError("one of --selection, --document-id or --collection is required")
    return Path(args.selection) if args.selection is not None else None


def _executor() -> TaskExecutor:
    return TaskExecutor(TaskStore(default_task_store_path()))


def _run_release_command(
    args: argparse.Namespace,
    config: HubConfig,
    review: WritingMaterialReviewService,
) -> dict[str, Any]:
    from qdrant_client import QdrantClient

    from knowledgehub.governance.snapshots import CollectionPromotionManager

    rag = config.rag_config("writing")
    index_root = Path(os.environ.get("KH_INDEX_ROOT", "/data/KnowledgeHub/indexes"))
    client = QdrantClient(url=rag.qdrant_url)
    promotion = CollectionPromotionManager(index_root, client)
    configured_fallback = config.knowledge_bases["writing"].collection
    promotion_status = promotion.status("writing", configured_fallback)
    current = promotion_status.get("current") or {}
    active_physical = str(current.get("active_collection") or configured_fallback)
    service = WritingMaterialReleaseService(
        review,
        QdrantReleaseBackend(client),
        config.writing_materials.data_root / "releases",
        promotion=promotion,
    )
    try:
        command = args.writing_material_release_command
        if command == "build":
            candidate_data_dir = (
                config.writing_materials.data_root
                / "release-candidates"
                / sha256_text(args.candidate_collection)[:24]
            )

            def merge(collection: str) -> dict[str, Any]:
                candidate_rag = rag.with_overrides(
                    data_dir=candidate_data_dir,
                    qdrant_collection=collection,
                )
                indexer = IncrementalChunkIndexer(
                    candidate_rag,
                    require_new_collection=False,
                )
                try:
                    return WritingMaterialCandidateIndexer(review, rag).build(
                        args.run_id,
                        candidate_collection=collection,
                        active_collection=active_physical,
                        candidate_data_dir=candidate_data_dir,
                        indexer=indexer,
                    )
                finally:
                    indexer.close()

            def operation() -> dict[str, Any]:
                return service.build(
                    args.run_id,
                    active_collection=active_physical,
                    candidate_collection=args.candidate_collection,
                    candidate_data_dir=candidate_data_dir,
                    merge=merge,
                    dry_run=args.dry_run,
                )

            if args.dry_run:
                return operation()
            return _executor().execute(
                "writing_material_release_build",
                operation,
                knowledge_base="writing",
                version=args.run_id,
                inputs={
                    "run_id": args.run_id,
                    "active_collection": active_physical,
                    "candidate_collection": args.candidate_collection,
                    "accepted_manifest": str(review.accepted_dir(args.run_id) / "manifest.json"),
                },
                input_manifest=str(review.accepted_dir(args.run_id) / "manifest.json"),
                lock_keys=(f"release:writing:{args.candidate_collection}",),
                output_manifest=lambda value: str(value["manifest_path"]),
            )
        if command == "stage":
            return service.stage(args.manifest, confirmed=args.yes)
        if command == "promote":
            return service.promote(active_physical, confirmed=args.yes)
        if args.dry_run:
            if args.yes:
                raise ValueError("rollback --dry-run cannot be combined with --yes")
            return service.assess_rollback(promotion_status)
        return service.rollback(confirmed=args.yes)
    finally:
        client.close()


def _release_retirement_service(
    config: HubConfig,
) -> WritingMaterialReleaseRetirementService:
    from qdrant_client import QdrantClient

    from knowledgehub.governance.snapshots import CollectionPromotionManager

    rag = config.rag_config("writing")
    client = QdrantClient(url=rag.qdrant_url)
    index_root = Path(os.environ.get("KH_INDEX_ROOT", "/data/KnowledgeHub/indexes"))
    return WritingMaterialReleaseRetirementService(
        config.writing_materials.data_root,
        QdrantReleaseBackend(client),
        CollectionPromotionManager(index_root, client),
        fallback_collection=config.knowledge_bases["writing"].collection,
    )


def _run_pilot_retrieval_command(
    args: argparse.Namespace,
    config: HubConfig,
    review: WritingMaterialReviewService,
) -> dict[str, Any]:
    from knowledgehub.retrieval.models import SearchRequest
    from knowledgehub.services.search_api import build_retrieval

    candidate_report = _read_object(args.candidate_report)
    cases = _read_jsonl_objects(args.queries)
    collection = candidate_report.get("candidate_collection")
    data_dir = candidate_report.get("candidate_data_dir")
    if not isinstance(collection, str) or not collection:
        raise ValueError("candidate report lacks a collection")
    if not isinstance(data_dir, str) or not data_dir:
        raise ValueError("candidate report lacks a data directory")
    rag = config.rag_config("writing").with_overrides(
        qdrant_collection=collection,
        data_dir=Path(data_dir),
    )
    holder: dict[str, Any] = {}

    def query(text: str, top_k: int) -> PilotRetrievalOutcome:
        service = holder.get("service")
        if service is None:
            service = build_retrieval(rag)
            holder["service"] = service
        response = service.search(
            SearchRequest(
                query=text,
                knowledge_base="writing",
                mode=args.mode,
                limit=top_k,
                prefetch_limit=max(50, top_k),
                source=None,
                writing_asset_type=infer_writing_asset_type(text),
            )
        )
        return PilotRetrievalOutcome(
            collection=response.collection,
            hits=tuple(dict(hit.payload) for hit in response.hits),
            warnings=tuple(response.warnings),
        )

    try:
        return CandidateRetrievalEvaluator(review).evaluate(
            args.run_id,
            candidate_report=candidate_report,
            cases=cases,
            query=query,
        )
    finally:
        service = holder.get("service")
        if service is not None:
            endpoint_pool = getattr(service, "endpoint_pool", None)
            if endpoint_pool is not None and hasattr(endpoint_pool, "close"):
                endpoint_pool.close()
            reranker = getattr(service, "reranker", None)
            if reranker is not None and hasattr(reranker, "close"):
                reranker.close()


def _emit(value: Mapping[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return value


def _read_jsonl_objects(path: Path) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number} must contain one JSON object")
        values.append(value)
    if not values:
        raise ValueError(f"{path} contains no retrieval cases")
    return values
