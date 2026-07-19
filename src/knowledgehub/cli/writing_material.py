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
from knowledgehub.governance.validation import HubValidator
from knowledgehub.hub.config import HubConfig
from knowledgehub.indexing.incremental import IncrementalChunkIndexer
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
        review = WritingMaterialReviewService(
            materials.data_root,
            materials.literature_data_dir,
        )
        if args.writing_material_command == "extract":
            selection = _selection_for_extract(args, review)
            pilot_approval = (
                _read_object(args.pilot_approval) if args.pilot_approval else None
            )
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
                                sha256_text(
                                    args.pilot_approval.read_text(encoding="utf-8")
                                )
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
                            "packet_sha256": sha256_text(
                                args.packet.read_text(encoding="utf-8")
                            ),
                            "decisions": str(args.decisions.resolve()),
                            "decisions_sha256": sha256_text(
                                args.decisions.read_text(encoding="utf-8")
                            ),
                            "confirmed": args.yes,
                        },
                        input_manifest=str(args.decisions.resolve()),
                        lock_keys=(f"review:writing-materials:{args.run_id}",),
                        output_manifest=lambda value: str(
                            value["quality_review_receipt"]["path"]
                        ),
                    )
            _emit(result)
            return 0
        if args.writing_material_command == "validate":
            result = HubValidator.writing_material_run(
                materials.data_root,
                materials.literature_data_dir,
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
        return service.rollback(confirmed=args.yes)
    finally:
        client.close()


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
