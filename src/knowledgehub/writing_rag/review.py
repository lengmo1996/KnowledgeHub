"""Append-only review, accepted snapshots and isolated candidate indexing."""

from __future__ import annotations

import json
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from knowledgehub.core.atomic import atomic_write_json, atomic_write_jsonl, atomic_write_text
from knowledgehub.core.hashing import sha256_json, sha256_text
from knowledgehub.core.models import KnowledgeDocument
from knowledgehub.indexing.incremental import IncrementalChunkIndexer, IndexInput
from knowledgehub.pipeline.models import ChunkRecord
from knowledgehub.writing_rag.materials import (
    RISK_FLAGS,
    SUPPORTED_ABSTRACTION_SCHEMA_VERSIONS,
    SUPPORTED_CLASSIFICATION_SCHEMA_VERSIONS,
    TAXONOMY,
    TAXONOMY_VERSION,
    MaterialValidationError,
    validate_stored_record,
)
from knowledgehub.writing_rag.provenance import ProvenanceDocumentReader, ProvenanceError

REVIEW_SCHEMA_VERSION = "writing-material-review-v1"
REVIEW_STATUS_SCHEMA_VERSION = "writing-material-review-status-v1"
ACCEPTED_SCHEMA_VERSION = "writing-material-accepted-v2"
ACCEPTED_POINTER_SCHEMA_VERSION = "writing-material-accepted-pointer-v1"
QUALITY_REVIEW_PACKET_SCHEMA_VERSION = "writing-material-quality-review-packet-v1"
QUALITY_REVIEW_RECEIPT_SCHEMA_VERSION = "writing-material-quality-review-receipt-v1"
INDEX_PROCESSOR_VERSION = "writing-material-index-v4"
CANDIDATE_SCHEMA_VERSION = "writing-material-candidate-v1"
_INDEX_CHUNK_NAMESPACE = uuid.UUID("f67b77bb-f6fc-56e6-9c37-620f3223bbb1")
_DECISIONS = {"accepted", "edited", "rejected"}
_FILES = {
    "evidence": "evidence.jsonl",
    "strategy": "strategies.jsonl",
    "template": "templates.jsonl",
    "phrase": "phrases.jsonl",
}
_ID_FIELDS = {
    "evidence": "evidence_id",
    "strategy": "strategy_id",
    "template": "template_id",
    "phrase": "phrase_id",
}
_EDITABLE = {
    "strategy": {
        "category",
        "label",
        "description",
        "steps",
        "applicability",
        "claim_strength_guidance",
        "explanation_zh",
        "explanation_en",
        "risk_flags",
        "quality_score",
    },
    "template": {
        "category",
        "template_text",
        "slots",
        "constraints",
        "claim_strength_guidance",
        "quality_score",
    },
    "phrase": {
        "category",
        "text",
        "function",
        "position",
        "register",
        "claim_strength",
        "constraints",
        "quality_score",
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReviewValidationError(ValueError):
    """An invalid, stale or provenance-breaking review operation."""


class WritingMaterialReviewService:
    def __init__(
        self,
        data_root: Path,
        literature_data_dir: Path,
    ) -> None:
        self.data_root = data_root
        self.reader = ProvenanceDocumentReader(literature_data_dir)

    def run_dir(self, run_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id):
            raise ReviewValidationError("invalid run ID")
        path = self.data_root / "runs" / run_id
        if not path.is_dir():
            raise ReviewValidationError(f"writing-material run is missing: {run_id}")
        return path

    def accepted_dir(self, run_id: str) -> Path:
        """Resolve the immutable complete snapshot for the latest review events."""

        run_dir = self.run_dir(run_id)
        records = self._records(run_dir)
        targets = self._targets(records)
        events = _read_jsonl(run_dir / "review-events.jsonl", required=False)
        self._assert_events(events, targets)
        projection = self._review_projection(records, events, targets)
        if _review_status_counts(projection)["pending"]:
            raise ReviewValidationError("complete accepted snapshot has pending review targets")
        selected = self._resolve_snapshot_dir(
            run_dir,
            events=events,
            projection=projection,
            completeness="complete",
        )
        if selected is None:
            raise ReviewValidationError("complete accepted snapshot is missing or stale")
        return selected

    def render(self, run_id: str) -> dict[str, Any]:
        run_dir = self.run_dir(run_id)
        self._completed_manifest(run_dir)
        records = self._records(run_dir)
        targets = self._targets(records)
        events = _read_jsonl(run_dir / "review-events.jsonl", required=False)
        self._assert_events(events, targets)
        projection = self._review_projection(records, events, targets)
        markdown = _render_records(records)
        markdown += _review_status_markdown(projection)
        markdown += self._decision_instructions(records)
        output = run_dir / "review.md"
        atomic_write_text(output, markdown, mode=0o600)
        projection_path = run_dir / "review-status.jsonl"
        atomic_write_jsonl(projection_path, projection, mode=0o600)
        counts = _review_status_counts(projection)
        return {
            "status": "success",
            "run_id": run_id,
            "review_report": str(output),
            "review_status": str(projection_path),
            "records": sum(len(values) for values in records.values()),
            "review_counts": counts,
        }

    def apply(
        self, run_id: str, decisions_path: Path, *, allow_partial_snapshot: bool = False
    ) -> dict[str, Any]:
        run_dir = self.run_dir(run_id)
        preflight = self.validate(run_id, verify_source=True)
        if preflight["status"] not in {"success", "partial"}:
            raise ReviewValidationError("run validation failed before applying review decisions")
        records = self._records(run_dir)
        targets = self._targets(records)
        existing = _read_jsonl(run_dir / "review-events.jsonl", required=False)
        new_events: list[dict[str, Any]] = []
        seen_assets: set[str] = set()
        latest_existing = {
            str(event.get("asset_id") or ""): event
            for event in existing
            if str(event.get("asset_id") or "") in targets
        }
        duplicates_ignored = 0
        for line_number, decision in enumerate(_read_jsonl(decisions_path), 1):
            event = self._validate_decision(decision, targets, line_number)
            asset_id = str(event["asset_id"])
            if asset_id in seen_assets:
                raise ReviewValidationError("decision manifest contains duplicate asset decisions")
            seen_assets.add(asset_id)
            prior = latest_existing.get(asset_id)
            if prior is not None and _semantic_event(prior) == _semantic_event(event):
                duplicates_ignored += 1
                continue
            new_events.append(event)
        if not new_events and not duplicates_ignored:
            raise ReviewValidationError("decision manifest contains no decisions")
        combined = [*existing, *new_events]
        projection = self._review_projection(records, combined, targets)
        pending = _review_status_counts(projection)["pending"]
        if pending and not allow_partial_snapshot:
            raise ReviewValidationError(
                f"review is incomplete ({pending} pending); explicitly allow a partial snapshot"
            )
        if new_events and self._has_snapshot_history(run_dir):
            snapshot = self._materialize_events(
                run_id,
                records=records,
                events=combined,
                allow_partial=allow_partial_snapshot,
                activate=False,
            )
            atomic_write_jsonl(run_dir / "review-events.jsonl", combined, mode=0o600)
            projection = self._review_projection(records, combined, targets)
            atomic_write_jsonl(run_dir / "review-status.jsonl", projection, mode=0o600)
            self._activate_snapshot(run_dir, Path(str(snapshot["path"])))
        else:
            if new_events:
                atomic_write_jsonl(run_dir / "review-events.jsonl", combined, mode=0o600)
            snapshot = self.materialize(run_id, allow_partial=allow_partial_snapshot)
        return {
            "status": "success",
            "run_id": run_id,
            "events_appended": len(new_events),
            "duplicate_events_ignored": duplicates_ignored,
            "review_events": str(run_dir / "review-events.jsonl"),
            "accepted_snapshot": snapshot,
        }

    def apply_quality_review(
        self,
        run_id: str,
        *,
        packet_path: Path,
        decisions_path: Path,
        dry_run: bool = False,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Validate a quality packet and import one explicit decision per flagged asset."""

        validation = self.validate(run_id, verify_source=True)
        if validation.get("status") != "success" or not validation.get("index_eligible"):
            raise ReviewValidationError(
                "quality review import requires a source-verified complete accepted snapshot"
            )
        accepted_dir = self.accepted_dir(run_id)
        accepted_manifest_path = accepted_dir / "manifest.json"
        accepted_manifest_sha256 = sha256_text(accepted_manifest_path.read_text(encoding="utf-8"))
        packet = self._validate_quality_packet(
            run_id,
            packet_path=packet_path,
            accepted_manifest_sha256=accepted_manifest_sha256,
        )
        records = self._records(self.run_dir(run_id))
        targets = self._targets(records)
        accepted_assets = self._accepted_asset_map(accepted_dir)
        raw_decisions = _read_jsonl(decisions_path)
        packet_items = {
            str(item["asset_id"]): item for item in packet["items"] if isinstance(item, Mapping)
        }
        if len(raw_decisions) != len(packet_items):
            raise ReviewValidationError(
                "quality review decisions must cover every packet item exactly once"
            )
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        reviewer = str(packet["reviewer"])
        for line_number, value in enumerate(raw_decisions, start=1):
            asset_id = str(value.get("asset_id") or "")
            if asset_id in seen:
                raise ReviewValidationError(
                    f"quality review decision line {line_number} duplicates an asset"
                )
            seen.add(asset_id)
            item = packet_items.get(asset_id)
            if item is None:
                raise ReviewValidationError(
                    f"quality review decision line {line_number} is outside the packet"
                )
            if value.get("reviewer") != reviewer:
                raise ReviewValidationError(
                    f"quality review decision line {line_number} reviewer differs from packet"
                )
            if value.get("based_on_hash") != item.get("based_on_hash"):
                raise ReviewValidationError(
                    f"quality review decision line {line_number} differs from packet target"
                )
            self._validate_decision(value, targets, line_number)
            normalized_value = self._normalize_quality_decision(
                value,
                accepted=accepted_assets[asset_id],
                target=targets[asset_id],
            )
            self._validate_decision(normalized_value, targets, line_number)
            normalized.append(normalized_value)
        if seen != set(packet_items):
            raise ReviewValidationError(
                "quality review decisions must cover every packet item exactly once"
            )

        decision_counts = dict(
            sorted(Counter(str(value["decision"]) for value in normalized).items())
        )
        base_result: dict[str, Any] = {
            "schema_name": "writing_material_quality_review_import",
            "schema_version": "writing-material-quality-review-import-v1",
            "status": "planned" if dry_run else "success",
            "run_id": run_id,
            "packet_path": str(packet_path.resolve()),
            "packet_fingerprint": packet["artifact_fingerprint"],
            "decisions_path": str(decisions_path.resolve()),
            "decisions_sha256": sha256_text(decisions_path.read_text(encoding="utf-8")),
            "accepted_manifest_sha256": accepted_manifest_sha256,
            "decision_counts": decision_counts,
            "decision_count": len(normalized),
            "source_verified": True,
            "review_events_modified": False,
            "accepted_snapshot_modified": False,
            "index_modified": False,
            "llm_called": False,
            "writes_performed": False,
        }
        if dry_run:
            base_result["artifact_fingerprint"] = sha256_json(base_result)
            return base_result
        if not confirmed:
            raise ReviewValidationError(
                "quality review import requires explicit confirmation (--yes)"
            )

        normalized_path = self.run_dir(run_id) / (
            f".quality-review-import-{uuid.uuid4().hex}.jsonl"
        )
        try:
            atomic_write_jsonl(normalized_path, normalized, mode=0o600)
            applied = self.apply(run_id, normalized_path)
        finally:
            normalized_path.unlink(missing_ok=True)
        after = self.validate(run_id, verify_source=True)
        receipt = self._write_quality_review_receipt(
            run_id,
            packet=packet,
            decisions_path=decisions_path,
            decisions=normalized,
        )
        base_result.update(
            {
                "review_events_modified": bool(applied["events_appended"]),
                "accepted_snapshot_modified": not bool(applied["accepted_snapshot"].get("reused")),
                "writes_performed": True,
                "accepted_snapshot": applied["accepted_snapshot"],
                "review_counts": after["review_counts"],
                "imported": applied["events_appended"],
                "idempotent": not bool(applied["events_appended"]),
                "quality_review_receipt": receipt,
            }
        )
        base_result["artifact_fingerprint"] = sha256_json(base_result)
        return base_result

    def reconcile_quality_review_receipt(
        self,
        run_id: str,
        *,
        packet_path: Path,
        decisions_path: Path,
        dry_run: bool = False,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Record a missing receipt after proving a historical quality import was applied."""

        validation = self.validate(run_id, verify_source=True)
        if validation.get("status") != "success" or not validation.get("index_eligible"):
            raise ReviewValidationError(
                "quality receipt reconciliation requires a source-verified complete snapshot"
            )
        packet = self._validate_quality_packet(
            run_id,
            packet_path=packet_path,
            accepted_manifest_sha256=None,
        )
        accepted_dir = self.accepted_dir(run_id)
        accepted_assets = self._accepted_asset_map(accepted_dir)
        records = self._records(self.run_dir(run_id))
        targets = self._targets(records)
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        packet_items = {
            str(item["asset_id"]): item
            for item in packet["items"]
            if isinstance(item, Mapping)
        }
        decisions = _read_jsonl(decisions_path)
        if len(decisions) != len(packet_items):
            raise ReviewValidationError(
                "quality review decisions must cover every packet item exactly once"
            )
        for line_number, value in enumerate(decisions, start=1):
            asset_id = str(value.get("asset_id") or "")
            item = packet_items.get(asset_id)
            if asset_id in seen or item is None:
                raise ReviewValidationError(
                    "quality receipt decisions contain duplicate or unknown assets"
                )
            seen.add(asset_id)
            if (
                value.get("reviewer") != packet.get("reviewer")
                or value.get("based_on_hash") != item.get("based_on_hash")
            ):
                raise ReviewValidationError("quality receipt decision differs from packet")
            self._validate_decision(value, targets, line_number)
            normalized_value = self._normalize_quality_decision(
                value,
                accepted=accepted_assets[asset_id],
                target=targets[asset_id],
            )
            self._validate_decision(normalized_value, targets, line_number)
            normalized.append(normalized_value)
        if seen != set(packet_items):
            raise ReviewValidationError(
                "quality review decisions must cover every packet item exactly once"
            )
        self._assert_quality_decisions_are_current(run_id, normalized)
        result: dict[str, Any] = {
            "schema_name": "writing_material_quality_review_receipt_reconciliation",
            "schema_version": QUALITY_REVIEW_RECEIPT_SCHEMA_VERSION,
            "status": "planned" if dry_run else "success",
            "run_id": run_id,
            "packet_fingerprint": packet["artifact_fingerprint"],
            "decision_count": len(normalized),
            "decision_counts": dict(
                sorted(Counter(str(value["decision"]) for value in normalized).items())
            ),
            "source_verified": True,
            "review_events_modified": False,
            "accepted_snapshot_modified": False,
            "index_modified": False,
            "llm_called": False,
            "writes_performed": False,
        }
        if dry_run:
            result["artifact_fingerprint"] = sha256_json(result)
            return result
        if not confirmed:
            raise ReviewValidationError(
                "quality receipt reconciliation requires explicit confirmation (--yes)"
            )
        receipt = self._write_quality_review_receipt(
            run_id,
            packet=packet,
            decisions_path=decisions_path,
            decisions=normalized,
        )
        result.update(
            {
                "quality_review_receipt": receipt,
                "writes_performed": not bool(receipt.get("reused")),
            }
        )
        result["artifact_fingerprint"] = sha256_json(result)
        return result

    def quality_acknowledgements(
        self,
        run_id: str,
        *,
        accepted_manifest_sha256: str,
    ) -> tuple[set[str], list[str]]:
        """Return reviewed asset IDs from receipts bound to the current snapshot."""

        receipt_dir = self.run_dir(run_id) / "quality-review-receipts"
        if not receipt_dir.exists():
            return set(), []
        current_manifest = _read_json(self.accepted_dir(run_id) / "manifest.json")
        targets = self._targets(self._records(self.run_dir(run_id)))
        acknowledged: set[str] = set()
        fingerprints: list[str] = []
        for path in sorted(receipt_dir.glob("*.json")):
            receipt = _read_json(path)
            fingerprinted = dict(receipt)
            fingerprint = fingerprinted.pop("artifact_fingerprint", None)
            reviewed_assets = receipt.get("reviewed_assets")
            decision_counts = receipt.get("decision_counts")
            if (
                fingerprint != sha256_json(fingerprinted)
                or receipt.get("schema_name") != "writing_material_quality_review_receipt"
                or receipt.get("schema_version") != QUALITY_REVIEW_RECEIPT_SCHEMA_VERSION
                or receipt.get("run_id") != run_id
                or not isinstance(receipt.get("reviewer"), str)
                or not str(receipt["reviewer"]).strip()
                or not isinstance(reviewed_assets, list)
                or not reviewed_assets
                or not isinstance(decision_counts, Mapping)
                or dict(decision_counts)
                != dict(
                    sorted(
                        Counter(str(item.get("decision")) for item in reviewed_assets).items()
                    )
                )
                or any(
                    not isinstance(item, Mapping)
                    or not isinstance(item.get("asset_id"), str)
                    or item.get("decision") not in _DECISIONS
                    or not isinstance(item.get("based_on_hash"), str)
                    or item.get("asset_id") not in targets
                    or item.get("based_on_hash")
                    != sha256_json(targets[str(item["asset_id"])][1])
                    for item in reviewed_assets
                )
                or len({str(item["asset_id"]) for item in reviewed_assets})
                != len(reviewed_assets)
            ):
                raise ReviewValidationError("quality review receipt is invalid")
            if (
                receipt.get("resulting_accepted_manifest_sha256")
                != accepted_manifest_sha256
            ):
                continue
            if (
                receipt.get("resulting_review_events_hash")
                != current_manifest.get("review_events_hash")
            ):
                raise ReviewValidationError("quality review receipt event binding is invalid")
            acknowledged.update(str(item["asset_id"]) for item in reviewed_assets)
            fingerprints.append(str(fingerprint))
        return acknowledged, fingerprints

    def _write_quality_review_receipt(
        self,
        run_id: str,
        *,
        packet: Mapping[str, Any],
        decisions_path: Path,
        decisions: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        self._assert_quality_decisions_are_current(run_id, decisions)
        run_dir = self.run_dir(run_id)
        accepted_dir = self.accepted_dir(run_id)
        manifest_path = accepted_dir / "manifest.json"
        packet_fingerprint = str(packet["artifact_fingerprint"])
        receipt_dir = run_dir / "quality-review-receipts"
        receipt_path = receipt_dir / f"{packet_fingerprint}.json"
        if receipt_path.exists():
            existing_receipt = _read_json(receipt_path)
            fingerprinted = dict(existing_receipt)
            fingerprint = fingerprinted.pop("artifact_fingerprint", None)
            if fingerprint != sha256_json(fingerprinted):
                raise ReviewValidationError("existing quality review receipt is invalid")
            return existing_receipt | {"path": str(receipt_path), "reused": True}
        receipt: dict[str, Any] = {
            "schema_name": "writing_material_quality_review_receipt",
            "schema_version": QUALITY_REVIEW_RECEIPT_SCHEMA_VERSION,
            "run_id": run_id,
            "reviewer": packet["reviewer"],
            "packet_fingerprint": packet_fingerprint,
            "quality_audit_fingerprint": packet["quality_audit_fingerprint"],
            "packet_accepted_manifest_sha256": packet["accepted_manifest_sha256"],
            "decisions_sha256": sha256_text(decisions_path.read_text(encoding="utf-8")),
            "decision_counts": dict(
                sorted(Counter(str(value["decision"]) for value in decisions).items())
            ),
            "reviewed_assets": sorted(
                (
                    {
                        "asset_id": value["asset_id"],
                        "decision": value["decision"],
                        "based_on_hash": value["based_on_hash"],
                    }
                    for value in decisions
                ),
                key=lambda value: str(value["asset_id"]),
            ),
            "resulting_accepted_manifest_sha256": sha256_text(
                manifest_path.read_text(encoding="utf-8")
            ),
            "resulting_review_events_hash": _read_json(manifest_path)["review_events_hash"],
            "recorded_at": _now(),
            "evidence_text_included": False,
            "material_text_included": False,
            "index_modified": False,
            "llm_called": False,
        }
        receipt["artifact_fingerprint"] = sha256_json(receipt)
        receipt_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write_json(receipt_path, receipt, mode=0o600)
        return receipt | {"path": str(receipt_path), "reused": False}

    def _assert_quality_decisions_are_current(
        self,
        run_id: str,
        decisions: Sequence[Mapping[str, Any]],
    ) -> None:
        events = _read_jsonl(self.run_dir(run_id) / "review-events.jsonl")
        latest = {str(event["asset_id"]): event for event in events}
        for value in decisions:
            event = latest.get(str(value["asset_id"]))
            expected = {
                "asset_id": value["asset_id"],
                "asset_type": event.get("asset_type") if event else None,
                "decision": value["decision"],
                "based_on_hash": value["based_on_hash"],
                "reviewer": str(value["reviewer"]).strip(),
                "reason": str(value["reason"]).strip(),
                "edits": dict(value.get("edits") or {}),
            }
            if event is None or _semantic_event(event) != expected:
                raise ReviewValidationError(
                    "quality receipt decisions are not the latest applied review events"
                )

    def materialize(self, run_id: str, *, allow_partial: bool = False) -> dict[str, Any]:
        run_dir = self.run_dir(run_id)
        self._completed_manifest(run_dir)
        records = self._records(run_dir)
        events = _read_jsonl(run_dir / "review-events.jsonl", required=False)
        return self._materialize_events(
            run_id,
            records=records,
            events=events,
            allow_partial=allow_partial,
            activate=True,
        )

    def _materialize_events(
        self,
        run_id: str,
        *,
        records: Mapping[str, Sequence[Mapping[str, Any]]],
        events: Sequence[Mapping[str, Any]],
        allow_partial: bool,
        activate: bool,
    ) -> dict[str, Any]:
        run_dir = self.run_dir(run_id)
        targets = self._targets(records)
        self._assert_events(events, targets)
        projection = self._review_projection(records, events, targets)
        review_counts = _review_status_counts(projection)
        pending = review_counts["pending"]
        if pending and not allow_partial:
            raise ReviewValidationError(
                f"review is incomplete ({pending} pending); partial materialization is not enabled"
            )
        completeness = "partial" if pending else "complete"
        accepted = self._accepted_records(records, events, targets, projection=projection)
        current = self._resolve_snapshot_dir(
            run_dir,
            events=events,
            projection=projection,
            completeness=completeness,
        )
        if current is not None:
            manifest = _read_json(current / "manifest.json")
            if activate:
                atomic_write_jsonl(run_dir / "review-status.jsonl", projection, mode=0o600)
                self._activate_snapshot(run_dir, current)
            return manifest | {
                "path": str(current),
                "index_eligible": completeness == "complete",
                "reused": True,
            }

        legacy_name = "accepted-partial" if pending else "accepted"
        legacy_dir = run_dir / legacy_name
        revision_id = self._snapshot_revision_id(events, projection, completeness)
        if not legacy_dir.exists():
            accepted_dir = legacy_dir
            revision_kind = "legacy"
        else:
            revision_root = run_dir / f"{legacy_name}-revisions"
            revision_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            accepted_dir = revision_root / revision_id
            revision_kind = "versioned"
        if accepted_dir.exists():
            raise ReviewValidationError("accepted snapshot revision collision")
        accepted_dir.mkdir(parents=True, mode=0o700)
        for asset_type, filename in _FILES.items():
            atomic_write_jsonl(accepted_dir / filename, accepted[asset_type], mode=0o600)
        manifest = {
            "schema_version": ACCEPTED_SCHEMA_VERSION,
            "run_id": run_id,
            "source_manifest_hash": sha256_text(
                (run_dir / "manifest.json").read_text(encoding="utf-8")
            ),
            "review_events_hash": sha256_json(events),
            "review_projection_hash": sha256_json(projection),
            "review_completeness": completeness,
            "target_count": len(targets),
            "review_counts": review_counts,
            "pending_count": pending,
            "dependency_exclusion_count": sum(
                value["snapshot_eligibility"] == "excluded_dependency" for value in projection
            ),
            "counts": {key: len(values) for key, values in accepted.items()},
            "materialized_at": _now(),
            "revision_id": revision_id,
            "revision_kind": revision_kind,
            "immutable_snapshot": True,
        }
        atomic_write_json(accepted_dir / "manifest.json", manifest, mode=0o600)
        atomic_write_jsonl(accepted_dir / "review-status.jsonl", projection, mode=0o600)
        if activate:
            atomic_write_jsonl(run_dir / "review-status.jsonl", projection, mode=0o600)
            self._activate_snapshot(run_dir, accepted_dir)
        return manifest | {
            "path": str(accepted_dir),
            "index_eligible": completeness == "complete",
            "reused": False,
        }

    @staticmethod
    def _snapshot_revision_id(
        events: Sequence[Mapping[str, Any]],
        projection: Sequence[Mapping[str, Any]],
        completeness: str,
    ) -> str:
        fingerprint = sha256_json(
            {
                "review_events_hash": sha256_json(events),
                "review_projection_hash": sha256_json(projection),
                "review_completeness": completeness,
            }
        )
        return f"rev-{fingerprint[:24]}"

    @staticmethod
    def _has_snapshot_history(run_dir: Path) -> bool:
        return any(
            path.exists()
            for path in (
                run_dir / "accepted",
                run_dir / "accepted-partial",
                run_dir / "accepted-revisions",
                run_dir / "accepted-partial-revisions",
            )
        )

    def _resolve_snapshot_dir(
        self,
        run_dir: Path,
        *,
        events: Sequence[Mapping[str, Any]],
        projection: Sequence[Mapping[str, Any]],
        completeness: str,
    ) -> Path | None:
        legacy_name = "accepted" if completeness == "complete" else "accepted-partial"
        revision_id = self._snapshot_revision_id(events, projection, completeness)
        expected_events_hash = sha256_json(events)
        expected_projection_hash = sha256_json(projection)
        candidates = (
            run_dir / legacy_name,
            run_dir / f"{legacy_name}-revisions" / revision_id,
        )
        for candidate in candidates:
            manifest_path = candidate / "manifest.json"
            if not manifest_path.is_file():
                continue
            try:
                manifest = _read_json(manifest_path)
            except (ReviewValidationError, json.JSONDecodeError, OSError):
                continue
            if (
                manifest.get("schema_version") == ACCEPTED_SCHEMA_VERSION
                and manifest.get("run_id") == run_dir.name
                and manifest.get("review_completeness") == completeness
                and manifest.get("review_events_hash") == expected_events_hash
                and manifest.get("review_projection_hash") == expected_projection_hash
            ):
                return candidate
        return None

    def _activate_snapshot(self, run_dir: Path, snapshot_dir: Path) -> None:
        resolved_run = run_dir.resolve()
        resolved_snapshot = snapshot_dir.resolve()
        if not resolved_snapshot.is_relative_to(resolved_run):
            raise ReviewValidationError("accepted snapshot escapes its run directory")
        manifest_path = snapshot_dir / "manifest.json"
        manifest = _read_json(manifest_path)
        completeness = str(manifest.get("review_completeness") or "")
        if completeness not in {"complete", "partial"}:
            raise ReviewValidationError("accepted snapshot completeness is invalid")
        pointer_name = (
            "accepted-current.json"
            if completeness == "complete"
            else "accepted-partial-current.json"
        )
        pointer: dict[str, Any] = {
            "schema_version": ACCEPTED_POINTER_SCHEMA_VERSION,
            "run_id": run_dir.name,
            "review_completeness": completeness,
            "revision_id": manifest.get("revision_id", "legacy"),
            "snapshot_path": str(resolved_snapshot.relative_to(resolved_run)),
            "manifest_sha256": sha256_text(manifest_path.read_text(encoding="utf-8")),
            "review_events_hash": manifest.get("review_events_hash"),
            "review_projection_hash": manifest.get("review_projection_hash"),
        }
        pointer["artifact_fingerprint"] = sha256_json(pointer)
        atomic_write_json(run_dir / pointer_name, pointer, mode=0o600)

    def _accepted_pointer_errors(
        self,
        run_dir: Path,
        *,
        accepted_dir: Path,
        accepted_manifest: Mapping[str, Any],
    ) -> list[str]:
        completeness = str(accepted_manifest.get("review_completeness") or "")
        pointer_name = (
            "accepted-current.json"
            if completeness == "complete"
            else "accepted-partial-current.json"
        )
        pointer_path = run_dir / pointer_name
        if not pointer_path.exists():
            return []
        try:
            pointer = _read_json(pointer_path)
            fingerprinted = dict(pointer)
            fingerprint = fingerprinted.pop("artifact_fingerprint", None)
            expected_path = str(accepted_dir.resolve().relative_to(run_dir.resolve()))
            manifest_path = accepted_dir / "manifest.json"
            valid = bool(
                fingerprint == sha256_json(fingerprinted)
                and pointer.get("schema_version") == ACCEPTED_POINTER_SCHEMA_VERSION
                and pointer.get("run_id") == run_dir.name
                and pointer.get("review_completeness") == completeness
                and pointer.get("snapshot_path") == expected_path
                and pointer.get("manifest_sha256")
                == sha256_text(manifest_path.read_text(encoding="utf-8"))
                and pointer.get("review_events_hash") == accepted_manifest.get("review_events_hash")
                and pointer.get("review_projection_hash")
                == accepted_manifest.get("review_projection_hash")
            )
            return [] if valid else ["accepted snapshot current pointer is invalid or stale"]
        except (ReviewValidationError, json.JSONDecodeError, OSError, ValueError):
            return ["accepted snapshot current pointer is invalid or stale"]

    @staticmethod
    def _accepted_asset_map(accepted_dir: Path) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for asset_type in ("strategy", "template", "phrase"):
            field = _ID_FIELDS[asset_type]
            for value in _read_jsonl(accepted_dir / _FILES[asset_type]):
                result[str(value[field])] = value
        return result

    def _validate_quality_packet(
        self,
        run_id: str,
        *,
        packet_path: Path,
        accepted_manifest_sha256: str | None,
    ) -> dict[str, Any]:
        packet = _read_json(packet_path)
        fingerprinted = dict(packet)
        fingerprint = fingerprinted.pop("artifact_fingerprint", None)
        items = packet.get("items")
        counts = packet.get("counts")
        if (
            fingerprint != sha256_json(fingerprinted)
            or packet.get("schema_name") != "writing_material_quality_review_packet"
            or packet.get("schema_version") != QUALITY_REVIEW_PACKET_SCHEMA_VERSION
            or packet.get("status") != "success"
            or packet.get("run_id") != run_id
            or (
                accepted_manifest_sha256 is not None
                and packet.get("accepted_manifest_sha256") != accepted_manifest_sha256
            )
            or not isinstance(packet.get("accepted_manifest_sha256"), str)
            or not isinstance(packet.get("reviewer"), str)
            or not str(packet["reviewer"]).strip()
            or not isinstance(items, list)
            or not items
            or not isinstance(counts, Mapping)
            or counts.get("flagged_assets") != len(items)
            or packet.get("decision_import_ready") is not False
            or packet.get("requires_explicit_reviewer_decision") is not True
            or packet.get("evidence_text_included") is not False
            or packet.get("provenance_excerpt_included") is not False
            or packet.get("review_decisions_modified") is not False
            or packet.get("accepted_snapshot_modified") is not False
            or packet.get("index_modified") is not False
            or packet.get("llm_called") is not False
        ):
            raise ReviewValidationError("quality review packet is invalid or stale")
        records = self._records(self.run_dir(run_id))
        targets = self._targets(records)
        accepted_assets = self._accepted_asset_map(self.accepted_dir(run_id))
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, Mapping):
                raise ReviewValidationError("quality review packet item is invalid")
            asset_id = str(item.get("asset_id") or "")
            target = targets.get(asset_id)
            if (
                not asset_id
                or asset_id in seen
                or target is None
                or target[0] == "evidence"
                or asset_id not in accepted_assets
                or item.get("asset_type") != target[0]
                or item.get("based_on_hash") != sha256_json(target[1])
            ):
                raise ReviewValidationError("quality review packet item is invalid or stale")
            draft = item.get("decision_draft")
            if (
                not isinstance(draft, Mapping)
                or draft.get("asset_id") != asset_id
                or draft.get("based_on_hash") != item.get("based_on_hash")
                or draft.get("reviewer") != packet.get("reviewer")
                or draft.get("decision") is not None
                or draft.get("reason") is not None
            ):
                raise ReviewValidationError("quality review packet draft is not pristine")
            seen.add(asset_id)
        return packet

    @staticmethod
    def _normalize_quality_decision(
        value: Mapping[str, Any],
        *,
        accepted: Mapping[str, Any],
        target: tuple[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        asset_type, raw = target
        decision = str(value["decision"])
        if decision == "rejected":
            return dict(value)
        carried = {
            field: accepted[field]
            for field in _EDITABLE[asset_type]
            if field in accepted and accepted.get(field) != raw.get(field)
        }
        requested = value.get("edits")
        if isinstance(requested, Mapping):
            carried.update(dict(requested))
        normalized = dict(value)
        normalized["edits"] = carried
        normalized["decision"] = "edited" if carried else "accepted"
        if normalized["decision"] == "accepted":
            normalized.pop("edits", None)
        return normalized

    def validate(self, run_id: str, *, verify_source: bool = True) -> dict[str, Any]:
        run_dir = self.run_dir(run_id)
        errors: list[str] = []
        records: dict[str, list[dict[str, Any]]]
        try:
            records = self._records(run_dir)
        except (ReviewValidationError, json.JSONDecodeError, OSError) as exc:
            return {"status": "failed", "run_id": run_id, "errors": [str(exc)]}
        targets = self._targets(records)
        evidence_ids = {str(value["evidence_id"]) for value in records["evidence"]}
        evidence_categories = {
            str(value["evidence_id"]): str(value["category"])
            for value in records["evidence"]
        }
        try:
            manifest = self._completed_manifest(run_dir)
            expected = {
                "evidence": int(manifest.get("evidence", -1)),
                "strategy": int(manifest.get("strategies", -1)),
                "template": int(manifest.get("templates", -1)),
                "phrase": int(manifest.get("phrases", -1)),
            }
        except (ReviewValidationError, TypeError, ValueError) as exc:
            return {"status": "failed", "run_id": run_id, "errors": [str(exc)]}
        versions = manifest.get("versions")
        if not isinstance(versions, Mapping):
            errors.append("run manifest versions are invalid")
        else:
            expected_trace = {
                "taxonomy_version": versions.get("taxonomy"),
                "prompt_version": versions.get("prompt"),
                "analyzer_provider": versions.get("provider"),
                "analyzer_model": versions.get("model"),
            }
            for asset_type, values in records.items():
                expected_schema = (
                    versions.get("classification_schema")
                    if asset_type == "evidence"
                    else versions.get("abstraction_schema")
                )
                for value in values:
                    for field, expected_value in expected_trace.items():
                        if value.get(field) != expected_value:
                            errors.append(f"{asset_type} {field} differs from run manifest")
                    if value.get("response_schema_version") != expected_schema:
                        errors.append(f"{asset_type} response schema differs from run manifest")
        for asset_type, values in records.items():
            if expected[asset_type] != len(values):
                errors.append(f"{asset_type} count differs from run manifest")
            for value in values:
                if asset_type != "evidence":
                    references = value.get("evidence_ids")
                    if (
                        not isinstance(references, list)
                        or not references
                        or not set(references) <= evidence_ids
                    ):
                        errors.append(f"{asset_type} references unknown evidence")
                    elif value.get("category") not in {
                        evidence_categories[str(reference)] for reference in references
                    }:
                        errors.append(
                            f"{asset_type} category is unsupported by referenced evidence"
                        )
        if verify_source:
            errors.extend(self._verify_evidence_source(records["evidence"]))
        events = _read_jsonl(run_dir / "review-events.jsonl", required=False)
        decision_ids: set[str] = set()
        for event in events:
            target_id = str(event.get("asset_id") or "")
            target = targets.get(target_id)
            if target is None:
                errors.append(f"review event references unknown target: {target_id}")
                continue
            if event.get("based_on_hash") != sha256_json(target[1]):
                errors.append(f"review event is stale: {target_id}")
            try:
                self._validate_existing_event(event, target)
            except ReviewValidationError as exc:
                errors.append(f"invalid review event {target_id}: {exc}")
            decision_id = str(event.get("decision_id") or "")
            if decision_id in decision_ids:
                errors.append(f"duplicate review decision ID: {decision_id}")
            decision_ids.add(decision_id)
        projection = self._review_projection(records, events, targets)
        review_counts = _review_status_counts(projection)
        projection_path = run_dir / "review-status.jsonl"
        if projection_path.exists():
            try:
                if _read_jsonl(projection_path) != projection:
                    errors.append("review status projection differs from review events")
            except (ReviewValidationError, json.JSONDecodeError, OSError) as exc:
                errors.append(f"review status projection is invalid: {exc}")
        complete_snapshot = False
        expected_completeness = "partial" if review_counts["pending"] else "complete"
        accepted_dir = self._resolve_snapshot_dir(
            run_dir,
            events=events,
            projection=projection,
            completeness=expected_completeness,
        )
        if accepted_dir is None:
            if self._has_snapshot_history(run_dir):
                errors.append("current accepted snapshot is missing or stale")
        else:
            directory_name = accepted_dir.name
            expected_accepted = self._accepted_records(
                records, events, targets, projection=projection
            )
            try:
                actual_accepted = {
                    asset_type: _read_jsonl(accepted_dir / filename)
                    for asset_type, filename in _FILES.items()
                }
                accepted_manifest = _read_json(accepted_dir / "manifest.json")
                if accepted_manifest.get("schema_version") != ACCEPTED_SCHEMA_VERSION:
                    errors.append(f"{directory_name} snapshot schema is invalid")
                if accepted_manifest.get("review_completeness") != expected_completeness:
                    errors.append(f"{directory_name} snapshot completeness is invalid")
                if expected_completeness == "complete" and review_counts["pending"]:
                    errors.append("complete accepted snapshot has pending review targets")
                if expected_completeness == "partial" and not review_counts["pending"]:
                    errors.append("partial accepted snapshot has no pending review targets")
                if actual_accepted != expected_accepted:
                    errors.append("accepted snapshot differs from review decisions")
                if accepted_manifest.get("counts") != {
                    key: len(values) for key, values in expected_accepted.items()
                }:
                    errors.append("accepted snapshot counts are invalid")
                if accepted_manifest.get("source_manifest_hash") != sha256_text(
                    (run_dir / "manifest.json").read_text(encoding="utf-8")
                ):
                    errors.append("accepted snapshot source manifest hash is invalid")
                if accepted_manifest.get("review_events_hash") != sha256_json(events):
                    errors.append("accepted snapshot review hash is invalid")
                if accepted_manifest.get("review_projection_hash") != sha256_json(projection):
                    errors.append("accepted snapshot review projection hash is invalid")
                if accepted_manifest.get("review_counts") != review_counts:
                    errors.append("accepted snapshot review counts are invalid")
                if accepted_manifest.get("target_count") != len(targets):
                    errors.append("accepted snapshot target count is invalid")
                if expected_completeness == "complete":
                    complete_snapshot = True
                errors.extend(
                    self._accepted_pointer_errors(
                        run_dir,
                        accepted_dir=accepted_dir,
                        accepted_manifest=accepted_manifest,
                    )
                )
            except (ReviewValidationError, json.JSONDecodeError, OSError) as exc:
                errors.append(f"accepted snapshot is invalid: {exc}")
        extraction_status = str(manifest["status"])
        return {
            "status": extraction_status if not errors else "failed",
            "run_id": run_id,
            "errors": errors,
            "counts": {key: len(values) for key, values in records.items()},
            "review_counts": review_counts,
            "source_verified": verify_source,
            "extraction_status": extraction_status,
            "index_eligible": not errors and extraction_status == "success" and complete_snapshot,
        }

    def _records(self, run_dir: Path) -> dict[str, list[dict[str, Any]]]:
        records = {
            asset_type: _read_jsonl(run_dir / filename) for asset_type, filename in _FILES.items()
        }
        for asset_type, values in records.items():
            field = _ID_FIELDS[asset_type]
            identifiers: set[str] = set()
            for value in values:
                identifier = value.get(field)
                if not isinstance(identifier, str) or not identifier:
                    raise ReviewValidationError(f"{asset_type} record lacks {field}")
                if identifier in identifiers:
                    raise ReviewValidationError(f"duplicate {asset_type} ID: {identifier}")
                identifiers.add(identifier)
                try:
                    validate_stored_record(asset_type, value)
                except MaterialValidationError as exc:
                    raise ReviewValidationError(
                        f"invalid stored {asset_type} {identifier}: {exc}"
                    ) from exc
        return records

    @staticmethod
    def _completed_manifest(run_dir: Path) -> dict[str, Any]:
        manifest = _read_json(run_dir / "manifest.json")
        if (
            manifest.get("schema_name") != "writing_material_extraction_run"
            or manifest.get("schema_version") != "1.0"
        ):
            raise ReviewValidationError("unsupported extraction run manifest schema")
        status = manifest.get("status")
        if status not in {"success", "partial"}:
            raise ReviewValidationError("extraction run is not complete")
        if not isinstance(manifest.get("finished_at"), str) or not manifest["finished_at"].strip():
            raise ReviewValidationError("completed extraction run lacks finished_at")
        versions = manifest.get("versions")
        if not isinstance(versions, Mapping):
            raise ReviewValidationError("extraction run versions are missing")
        if versions.get("taxonomy") != TAXONOMY_VERSION:
            raise ReviewValidationError("unsupported extraction run taxonomy version")
        if versions.get("abstraction_schema") not in SUPPORTED_ABSTRACTION_SCHEMA_VERSIONS:
            raise ReviewValidationError(
                "unsupported extraction run abstraction_schema version"
            )
        if versions.get("classification_schema") not in SUPPORTED_CLASSIFICATION_SCHEMA_VERSIONS:
            raise ReviewValidationError(
                "unsupported extraction run classification_schema version"
            )
        for key in ("prompt", "provider", "model"):
            if not isinstance(versions.get(key), str) or not str(versions[key]).strip():
                raise ReviewValidationError(f"extraction run {key} version is missing")
        return manifest

    @staticmethod
    def _targets(
        records: Mapping[str, Sequence[Mapping[str, Any]]],
    ) -> dict[str, tuple[str, Mapping[str, Any]]]:
        result: dict[str, tuple[str, Mapping[str, Any]]] = {}
        for asset_type, values in records.items():
            field = _ID_FIELDS[asset_type]
            for value in values:
                result[str(value[field])] = (asset_type, value)
        return result

    @staticmethod
    def _accepted_records(
        records: Mapping[str, Sequence[Mapping[str, Any]]],
        events: Sequence[Mapping[str, Any]],
        targets: Mapping[str, tuple[str, Mapping[str, Any]]],
        *,
        projection: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        latest: dict[str, Mapping[str, Any]] = {}
        for event in events:
            target_id = str(event.get("asset_id") or "")
            if target_id in targets:
                latest[target_id] = event
        projected = {
            str(value["asset_id"]): value
            for value in (
                projection
                if projection is not None
                else WritingMaterialReviewService._review_projection(records, events, targets)
            )
        }
        accepted: dict[str, list[dict[str, Any]]] = {key: [] for key in _FILES}
        accepted_evidence: set[str] = set()
        for evidence in records["evidence"]:
            identifier = str(evidence["evidence_id"])
            selected_event = latest.get(identifier)
            if selected_event and selected_event.get("decision") == "accepted":
                materialized = dict(evidence)
                materialized.update(_review_audit(selected_event, evidence, materialized))
                accepted["evidence"].append(materialized)
                accepted_evidence.add(identifier)
        for asset_type in ("strategy", "template", "phrase"):
            for record in records[asset_type]:
                identifier = str(record[_ID_FIELDS[asset_type]])
                selected_event = latest.get(identifier)
                if not selected_event or selected_event.get("decision") not in {
                    "accepted",
                    "edited",
                }:
                    continue
                if projected[identifier]["snapshot_eligibility"] != "eligible":
                    continue
                references = set(str(value) for value in record.get("evidence_ids") or [])
                if not references or not references.issubset(accepted_evidence):
                    continue
                materialized = dict(record)
                if selected_event["decision"] == "edited":
                    materialized.update(dict(selected_event["edits"]))
                    validate_stored_record(asset_type, materialized)
                materialized.update(_review_audit(selected_event, record, materialized))
                accepted[asset_type].append(materialized)
        return accepted

    @staticmethod
    def _review_projection(
        records: Mapping[str, Sequence[Mapping[str, Any]]],
        events: Sequence[Mapping[str, Any]],
        targets: Mapping[str, tuple[str, Mapping[str, Any]]],
    ) -> list[dict[str, Any]]:
        latest: dict[str, Mapping[str, Any]] = {}
        for event in events:
            asset_id = str(event.get("asset_id") or "")
            if asset_id in targets:
                latest[asset_id] = event
        accepted_evidence = {
            asset_id
            for asset_id, (asset_type, _record) in targets.items()
            if asset_type == "evidence" and latest.get(asset_id, {}).get("decision") == "accepted"
        }
        projection: list[dict[str, Any]] = []
        for asset_type in _FILES:
            for record in records[asset_type]:
                asset_id = str(record[_ID_FIELDS[asset_type]])
                selected_event = latest.get(asset_id)
                status = str(selected_event["decision"]) if selected_event else "pending"
                eligibility = "excluded_decision"
                exclusion_reason: str | None = f"review_status_{status}"
                if asset_type == "evidence" and status == "accepted":
                    eligibility, exclusion_reason = "eligible", None
                elif asset_type != "evidence" and status in {"accepted", "edited"}:
                    references = {str(value) for value in record.get("evidence_ids") or []}
                    if references and references <= accepted_evidence:
                        eligibility, exclusion_reason = "eligible", None
                    else:
                        eligibility = "excluded_dependency"
                        exclusion_reason = "referenced_evidence_not_accepted"
                projection.append(
                    {
                        "schema_version": REVIEW_STATUS_SCHEMA_VERSION,
                        "asset_id": asset_id,
                        "asset_type": asset_type,
                        "based_on_hash": sha256_json(record),
                        "status": status,
                        "decision_id": selected_event.get("decision_id")
                        if selected_event
                        else None,
                        "reviewer": selected_event.get("reviewer") if selected_event else None,
                        "timestamp": selected_event.get("timestamp") if selected_event else None,
                        "reason": selected_event.get("reason") if selected_event else None,
                        "snapshot_eligibility": eligibility,
                        "exclusion_reason": exclusion_reason,
                    }
                )
        return projection

    @staticmethod
    def _assert_events(
        events: Sequence[Mapping[str, Any]],
        targets: Mapping[str, tuple[str, Mapping[str, Any]]],
    ) -> None:
        decision_ids: set[str] = set()
        for event in events:
            asset_id = str(event.get("asset_id") or "")
            target = targets.get(asset_id)
            if target is None:
                raise ReviewValidationError(f"review event references unknown target: {asset_id}")
            if event.get("based_on_hash") != sha256_json(target[1]):
                raise ReviewValidationError(f"review event is stale: {asset_id}")
            WritingMaterialReviewService._validate_existing_event(event, target)
            decision_id = str(event["decision_id"])
            if decision_id in decision_ids:
                raise ReviewValidationError(f"duplicate review decision ID: {decision_id}")
            decision_ids.add(decision_id)

    @staticmethod
    def _validate_decision(
        value: Mapping[str, Any],
        targets: Mapping[str, tuple[str, Mapping[str, Any]]],
        line_number: int,
    ) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ReviewValidationError(f"decision line {line_number} must be an object")
        allowed = {"asset_id", "decision", "based_on_hash", "reviewer", "reason", "edits"}
        unknown = set(value) - allowed
        required = {"asset_id", "decision", "based_on_hash", "reviewer", "reason"}
        if unknown or not required <= set(value):
            raise ReviewValidationError(f"decision line {line_number} has invalid fields")
        asset_id = value.get("asset_id")
        decision = value.get("decision")
        reviewer = value.get("reviewer")
        reason = value.get("reason")
        if not all(isinstance(item, str) and item.strip() for item in (asset_id, reviewer, reason)):
            raise ReviewValidationError(f"decision line {line_number} has empty identity fields")
        assert isinstance(asset_id, str)
        assert isinstance(reviewer, str)
        assert isinstance(reason, str)
        if decision not in _DECISIONS:
            raise ReviewValidationError(f"decision line {line_number} has invalid decision")
        target = targets.get(str(asset_id))
        if target is None:
            raise ReviewValidationError(f"decision line {line_number} references unknown asset")
        asset_type, record = target
        if value.get("based_on_hash") != sha256_json(record):
            raise ReviewValidationError(f"decision line {line_number} is stale")
        edits = value.get("edits")
        if decision == "edited":
            if asset_type == "evidence":
                raise ReviewValidationError("evidence is immutable; reject and re-extract it")
            if not isinstance(edits, Mapping) or not edits:
                raise ReviewValidationError("edited decisions require a non-empty edits object")
            WritingMaterialReviewService._validate_edits(asset_type, edits)
        elif edits not in (None, {}):
            raise ReviewValidationError("accepted/rejected decisions cannot contain edits")
        timestamp = _now()
        event_payload = {
            "asset_id": asset_id,
            "asset_type": asset_type,
            "decision": decision,
            "based_on_hash": value["based_on_hash"],
            "reviewer": reviewer.strip(),
            "reason": reason.strip(),
            "edits": dict(edits) if isinstance(edits, Mapping) else {},
            "timestamp": timestamp,
            "schema_version": REVIEW_SCHEMA_VERSION,
        }
        return {
            "decision_id": f"decision:{uuid.uuid4()}",
            **event_payload,
        }

    @staticmethod
    def _validate_existing_event(
        event: Mapping[str, Any], target: tuple[str, Mapping[str, Any]]
    ) -> None:
        required = {
            "decision_id",
            "asset_id",
            "asset_type",
            "decision",
            "based_on_hash",
            "reviewer",
            "reason",
            "edits",
            "timestamp",
            "schema_version",
        }
        if set(event) != required or event.get("schema_version") != REVIEW_SCHEMA_VERSION:
            raise ReviewValidationError("event schema is invalid")
        asset_type, _record = target
        if event.get("asset_type") != asset_type or event.get("decision") not in _DECISIONS:
            raise ReviewValidationError("event type or decision is invalid")
        for key in ("decision_id", "asset_id", "reviewer", "reason", "timestamp"):
            if not isinstance(event.get(key), str) or not str(event[key]).strip():
                raise ReviewValidationError(f"event {key} is invalid")
        edits = event.get("edits")
        if not isinstance(edits, Mapping):
            raise ReviewValidationError("event edits must be an object")
        if event["decision"] == "edited":
            if asset_type == "evidence" or not edits:
                raise ReviewValidationError("event attempts to edit immutable evidence")
            WritingMaterialReviewService._validate_edits(asset_type, edits)
        elif edits:
            raise ReviewValidationError("non-edited event contains edits")

    @staticmethod
    def _validate_edits(asset_type: str, edits: Mapping[str, Any]) -> None:
        unknown = set(edits) - _EDITABLE[asset_type]
        if unknown:
            raise ReviewValidationError(f"non-editable fields: {', '.join(sorted(unknown))}")
        if "category" in edits and edits["category"] not in TAXONOMY:
            raise ReviewValidationError("edited category is invalid")
        if "risk_flags" in edits and (
            not isinstance(edits["risk_flags"], list)
            or any(value not in RISK_FLAGS for value in edits["risk_flags"])
        ):
            raise ReviewValidationError("edited risk_flags are invalid")
        if "quality_score" in edits and (
            not isinstance(edits["quality_score"], (int, float))
            or isinstance(edits["quality_score"], bool)
            or not 0 <= float(edits["quality_score"]) <= 1
        ):
            raise ReviewValidationError("edited quality_score is invalid")
        for key, value in edits.items():
            if key in {"steps", "constraints", "risk_flags", "slots", "quality_score", "category"}:
                continue
            if not isinstance(value, str) or not value.strip():
                raise ReviewValidationError(f"edited {key} must be a non-empty string")
        for key in {"steps", "constraints"} & set(edits):
            if not isinstance(edits[key], list) or any(
                not isinstance(value, str) or not value.strip() for value in edits[key]
            ):
                raise ReviewValidationError(f"edited {key} must be a string array")
        if "slots" in edits and (
            not isinstance(edits["slots"], list)
            or any(
                not isinstance(slot, Mapping)
                or set(slot) != {"name", "semantic_type", "required"}
                or not isinstance(slot["name"], str)
                or not isinstance(slot["semantic_type"], str)
                or not isinstance(slot["required"], bool)
                for slot in edits["slots"]
            )
        ):
            raise ReviewValidationError("edited slots are invalid")

    def _verify_evidence_source(self, evidences: Sequence[Mapping[str, Any]]) -> list[str]:
        errors: list[str] = []
        cache: dict[str, Any] = {}
        for evidence in evidences:
            document_id = str(evidence.get("document_id") or "")
            try:
                if document_id not in cache:
                    cache[document_id] = self.reader.load(document_id)
                document = cache[document_id]
                if evidence.get("parse_fingerprint") != document.parse_fingerprint:
                    raise ReviewValidationError("parse fingerprint changed")
                if (
                    evidence.get("source_content_fingerprint")
                    != document.source_content_fingerprint
                ):
                    raise ReviewValidationError("source content fingerprint changed")
                if evidence.get("zotero_item_key") != document.zotero_item_key:
                    raise ReviewValidationError("Zotero item key changed")
                if evidence.get("attachment_key") != document.attachment_key:
                    raise ReviewValidationError("attachment key changed")
                paragraph_id = str(evidence.get("paragraph_id") or "")
                paragraph = next(
                    value for value in document.paragraphs if value.paragraph_id == paragraph_id
                )
                start, end = int(evidence["char_start"]), int(evidence["char_end"])
                if paragraph.text[start:end] != evidence.get("original_text"):
                    raise ReviewValidationError("exact span no longer matches source")
                if sha256_text(paragraph.text) != evidence.get("source_paragraph_hash"):
                    raise ReviewValidationError("source paragraph hash changed")
                expected = [
                    value.__dict__
                    if hasattr(value, "__dict__")
                    else {
                        "self_ref": value.self_ref,
                        "source_start": value.source_start,
                        "source_end": value.source_end,
                        "paragraph_start": value.paragraph_start,
                        "paragraph_end": value.paragraph_end,
                        "page_no": value.page_no,
                        "bbox": dict(value.bbox),
                    }
                    for value in paragraph.map_range(start, end)
                ]
                if expected != evidence.get("source_spans"):
                    raise ReviewValidationError("source segment mapping changed")
            except (
                ProvenanceError,
                ReviewValidationError,
                KeyError,
                ValueError,
                StopIteration,
            ) as exc:
                errors.append(f"{evidence.get('evidence_id')}: {exc}")
        return errors

    @staticmethod
    def _decision_instructions(records: Mapping[str, Sequence[Mapping[str, Any]]]) -> str:
        lines = [
            "",
            "# Decision manifest reference",
            "",
            "Each evidence and material asset requires an explicit decision. Use its exact based-on hash.",
            "Evidence may only be accepted or rejected; incorrect evidence must be re-extracted.",
            "",
        ]
        for asset_type, values in records.items():
            field = _ID_FIELDS[asset_type]
            for value in values:
                lines.append(f"- `{value[field]}` ({asset_type}): `{sha256_json(value)}`")
        return "\n".join(lines).rstrip() + "\n"


class WritingMaterialCandidateIndexer:
    """Build accepted abstractions into a brand-new physical Writing collection."""

    def __init__(self, review: WritingMaterialReviewService, rag_config: Any) -> None:
        self.review = review
        self.rag_config = rag_config

    def build(
        self,
        run_id: str,
        *,
        candidate_collection: str,
        active_collection: str,
        candidate_data_dir: Path,
        dry_run: bool = False,
        indexer: IncrementalChunkIndexer | None = None,
    ) -> dict[str, Any]:
        if not candidate_collection.strip() or candidate_collection == active_collection:
            raise ReviewValidationError("candidate collection must be a distinct physical name")
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", candidate_collection)
            or candidate_collection == "knowledgehub_writing_current"
        ):
            raise ReviewValidationError("candidate collection has an unsafe or alias name")
        validation = self.review.validate(run_id, verify_source=True)
        if validation["status"] != "success" or not validation.get("index_eligible"):
            raise ReviewValidationError(
                "only a complete successful extraction run may be candidate indexed"
            )
        accepted_dir = self.review.accepted_dir(run_id)
        if not (accepted_dir / "manifest.json").is_file():
            raise ReviewValidationError("complete accepted snapshot is missing")
        accepted_manifest = _read_json(accepted_dir / "manifest.json")
        if (
            accepted_manifest.get("schema_version") != ACCEPTED_SCHEMA_VERSION
            or accepted_manifest.get("review_completeness") != "complete"
            or accepted_manifest.get("pending_count") != 0
        ):
            raise ReviewValidationError("accepted snapshot is not complete or uses an old schema")
        evidences = {
            str(value["evidence_id"]): value
            for value in _read_jsonl(accepted_dir / "evidence.jsonl")
        }
        values: list[IndexInput] = []
        for asset_type in ("strategy", "template", "phrase"):
            for asset in _read_jsonl(accepted_dir / _FILES[asset_type]):
                values.append(_index_input(asset_type, asset, evidences))
        config = self.rag_config.with_overrides(
            data_dir=candidate_data_dir,
            qdrant_collection=candidate_collection,
        )
        selected_indexer = indexer or IncrementalChunkIndexer(
            config,
            initialize=not dry_run,
            require_new_collection=True,
        )
        try:
            result = selected_indexer.build(
                values,
                knowledge_base="writing",
                dry_run=dry_run,
                prune=False,
            ).to_dict()
        finally:
            if indexer is None:
                selected_indexer.close()
        result.update(
            {
                "schema_name": "writing_material_candidate",
                "schema_version": CANDIDATE_SCHEMA_VERSION,
                "run_id": run_id,
                "candidate_collection": candidate_collection,
                "candidate_data_dir": str(candidate_data_dir),
                "manifest_path": (
                    str(candidate_data_dir / "writing-material-candidate.json")
                    if not dry_run
                    else None
                ),
                "accepted_manifest": str(accepted_dir / "manifest.json"),
                "accepted_manifest_sha256": sha256_text(
                    (accepted_dir / "manifest.json").read_text(encoding="utf-8")
                ),
                "source_verified": True,
                "accepted_only": True,
                "promotion_performed": False,
            }
        )
        result["artifact_fingerprint"] = sha256_json(result)
        if not dry_run:
            atomic_write_json(
                candidate_data_dir / "writing-material-candidate.json",
                result,
                mode=0o600,
            )
        return result


def _semantic_event(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: event.get(key)
        for key in (
            "asset_id",
            "asset_type",
            "decision",
            "based_on_hash",
            "reviewer",
            "reason",
            "edits",
        )
    }


def _review_audit(
    event: Mapping[str, Any],
    original: Mapping[str, Any],
    materialized: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "review_status": event["decision"],
        "reviewed_from_hash": sha256_json(original),
        "review_decision_id": event["decision_id"],
        "review_reviewer": event["reviewer"],
        "review_timestamp": event["timestamp"],
        "review_reason": event["reason"],
        "materialized_hash": sha256_json(materialized),
    }


def _review_status_counts(projection: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    result = {"pending": 0, "accepted": 0, "edited": 0, "rejected": 0}
    for value in projection:
        status = str(value.get("status") or "")
        if status in result:
            result[status] += 1
    return result


def _review_status_markdown(projection: Sequence[Mapping[str, Any]]) -> str:
    counts = _review_status_counts(projection)
    exclusions = [
        value for value in projection if value.get("snapshot_eligibility") == "excluded_dependency"
    ]
    lines = [
        "",
        "# Review status",
        "",
        (
            f"Pending: {counts['pending']}; accepted: {counts['accepted']}; "
            f"edited: {counts['edited']}; rejected: {counts['rejected']}."
        ),
        "",
        f"Accepted dependency exclusions: {len(exclusions)}.",
        "",
    ]
    for value in exclusions:
        lines.append(f"- `{value['asset_id']}`: `{value['exclusion_reason']}`")
    return "\n".join(lines).rstrip() + "\n"


def _index_input(
    asset_type: str,
    asset: Mapping[str, Any],
    evidences: Mapping[str, Mapping[str, Any]],
) -> IndexInput:
    identifier = str(asset[_ID_FIELDS[asset_type]])
    references = [evidences[str(value)] for value in asset["evidence_ids"]]
    if asset_type == "strategy":
        text = "\n".join(
            [
                str(asset["label"]),
                str(asset["description"]),
                *[str(value) for value in asset.get("steps") or []],
                str(asset["applicability"]),
                str(asset["claim_strength_guidance"]),
            ]
        )
        title = str(asset["label"])
    elif asset_type == "template":
        text = "\n".join(
            [
                str(asset["template_text"]),
                *[str(value) for value in asset.get("constraints") or []],
                str(asset["claim_strength_guidance"]),
            ]
        )
        title = str(asset["template_text"])
    else:
        text = "\n".join(
            [
                str(asset["text"]),
                str(asset["function"]),
                str(asset["position"]),
                str(asset["register"]),
            ]
        )
        title = str(asset["text"])
    sparse_text = "\n".join(
        [
            text,
            f"material type: {asset_type}",
            f"retrieval intent: {asset_type} writing {asset_type} {asset_type} {asset_type}",
            f"writing category: {str(asset['category']).replace('_', ' ')}",
            f"language: {asset['language']}",
        ]
    )
    content_hash = sha256_text(text)
    provenance = [
        {
            "evidence_id": value["evidence_id"],
            "document_id": value["document_id"],
            "zotero_item_key": value["zotero_item_key"],
            "attachment_key": value["attachment_key"],
            "section": value["section_title"],
            "page_start": value["page_start"],
            "page_end": value["page_end"],
            "paragraph_id": value["paragraph_id"],
            "char_start": value["char_start"],
            "char_end": value["char_end"],
            "excerpt": str(value["original_text"])[:240],
        }
        for value in references
    ]
    metadata = {
        "asset_type": asset_type,
        "category": asset["category"],
        "language": asset["language"],
        "quality_score": asset["quality_score"],
        "evidence_ids": list(asset["evidence_ids"]),
        "provenance": provenance,
        "accepted_snapshot_only": True,
    }
    document = KnowledgeDocument(
        document_id=identifier,
        knowledge_base="writing",
        source_type="writing_material",
        title=title[:500],
        content_hash=content_hash,
        source_url=f"writing-material://{identifier}",
        retrieved_at=_now(),
        content=text,
        metadata=metadata,
    )
    chunk_identity = sha256_json({"asset": identifier, "content": content_hash})
    chunk = ChunkRecord(
        # Qdrant point IDs must be unsigned integers or UUIDs. Keep the
        # human-readable asset ID in document_id/payload and derive a stable
        # UUID solely for the physical point identity.
        chunk_id=str(uuid.uuid5(_INDEX_CHUNK_NAMESPACE, chunk_identity)),
        document_id=identifier,
        attachment_key=str(references[0]["attachment_key"]),
        chunk_index=0,
        text=text,
        text_sha256=content_hash,
        chunk_fingerprint=sha256_json(
            {"content": content_hash, "metadata": metadata, "processor": INDEX_PROCESSOR_VERSION}
        ),
        token_count=max(1, len(re.findall(r"[A-Za-z0-9-]+|[\u3400-\u4dbf\u4e00-\u9fff]", text))),
        sparse_text=sparse_text,
        page_start=min(int(value["page_start"]) for value in references),
        page_end=max(int(value["page_end"]) for value in references),
        section_path=(str(asset["category"]), asset_type),
        metadata=metadata,
    )
    return IndexInput(document=document, chunks=(chunk,), processor_version=INDEX_PROCESSOR_VERSION)


def _render_records(records: Mapping[str, Sequence[Mapping[str, Any]]]) -> str:
    related: dict[str, list[tuple[str, Mapping[str, Any]]]] = {}
    for asset_type in ("strategy", "template", "phrase"):
        for value in records[asset_type]:
            for evidence_id in value.get("evidence_ids") or []:
                related.setdefault(str(evidence_id), []).append((asset_type, value))
    lines = [
        "# Writing material review",
        "",
        "Evidence text is immutable. Reject incorrect provenance instead of editing it.",
        "",
    ]
    for evidence in sorted(
        records["evidence"],
        key=lambda value: (
            str(value.get("document_id")),
            str(value.get("section_title")),
            int(value.get("char_start", 0)),
        ),
    ):
        lines.extend(
            [
                f"## {evidence['section_title']} — {evidence['category']}",
                "",
                f"- Evidence ID: `{evidence['evidence_id']}`",
                f"- Document: `{evidence['document_id']}`",
                f"- Zotero item / attachment: `{evidence['zotero_item_key']}` / `{evidence['attachment_key']}`",
                f"- Page: {evidence['page_start']}-{evidence['page_end']}",
                f"- Paragraph range: `{evidence['paragraph_id']}[{evidence['char_start']}:{evidence['char_end']}]`",
                f"- Quality: {evidence['quality_score']}; risks: {', '.join(evidence.get('risk_flags') or []) or 'none'}",
                "",
                "> " + str(evidence["original_text"]).replace("\n", " "),
                "",
            ]
        )
        for asset_type, asset in related.get(str(evidence["evidence_id"]), []):
            identifier = asset[_ID_FIELDS[asset_type]]
            if asset_type == "strategy":
                description = f"{asset['label']} — {asset['description']}"
            elif asset_type == "template":
                description = f"`{asset['template_text']}`"
            else:
                description = f"`{asset['text']}`"
            lines.extend([f"- {asset_type.title()} `{identifier}`: {description}", ""])
    return "\n".join(lines).rstrip() + "\n"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ReviewValidationError(f"required file is missing: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ReviewValidationError(f"JSON root must be an object: {path}")
    return dict(value)


def _read_jsonl(path: Path, *, required: bool = True) -> list[dict[str, Any]]:
    if not path.is_file():
        if required:
            raise ReviewValidationError(f"required file is missing: {path}")
        return []
    result: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, Mapping):
            raise ReviewValidationError(f"JSONL line {line_number} must be an object: {path}")
        result.append(dict(value))
    return result
