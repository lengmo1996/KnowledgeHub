"""Cross-domain integrity checks that never repair or delete data implicitly."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from knowledgehub.core.hashing import sha256_file, sha256_json, sha256_text


class HubValidator:
    def __init__(
        self,
        code_root: Path,
        writing_root: Path,
        *,
        rag_dirs: Mapping[str, Path] | None = None,
    ) -> None:
        self.code_root = code_root
        self.writing_root = writing_root
        self.rag_dirs = dict(rag_dirs or {})

    def sources(self) -> dict[str, Any]:
        errors: list[str] = []
        checked = 0
        for marker in self.code_root.glob("sources/repositories/*/*/current.json"):
            checked += 1
            try:
                value = json.loads(marker.read_text(encoding="utf-8"))
                source = Path(str(value["source_path"]))
                if not source.is_dir() or len(str(value.get("commit") or "")) != 40:
                    errors.append(f"invalid source marker: {marker}")
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                errors.append(f"unreadable source marker: {marker}")
        return self._result("sources", checked, errors)

    def normalized(self) -> dict[str, Any]:
        errors: list[str] = []
        duplicates: set[str] = set()
        seen: set[str] = set()
        checked = 0
        for manifest in self.code_root.glob("normalized/**/*.jsonl"):
            if (
                manifest.parent == self.code_root / "normalized"
                and (manifest.parent / manifest.stem).is_dir()
            ):
                # V1 wrote one library-level manifest. V2 version-scoped
                # manifests supersede it without deleting the frozen file.
                continue
            for line_number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                checked += 1
                try:
                    value = json.loads(line)
                    document_id = str(value["document_id"])
                    metadata = value["metadata"]
                    if document_id in seen:
                        duplicates.add(document_id)
                    seen.add(document_id)
                    for field in ("library", "version", "source_type", "source_url", "commit"):
                        if field == "source_url":
                            present = value.get(field)
                        else:
                            present = metadata.get(field)
                        if not present:
                            errors.append(f"{manifest}:{line_number} missing {field}")
                    path = value.get("content_path")
                    if path and Path(path).is_file() and sha256_file(path) != value.get("content_hash"):
                        errors.append(f"{manifest}:{line_number} content hash mismatch")
                except (ValueError, KeyError, json.JSONDecodeError):
                    errors.append(f"{manifest}:{line_number} invalid record")
        errors.extend(f"duplicate document_id: {value}" for value in sorted(duplicates))
        return self._result("normalized", checked, errors)

    def dependencies(self) -> dict[str, Any]:
        """Validate pinned dependency evidence without resolving or installing it."""
        errors: list[str] = []
        checked = 0
        seen: set[tuple[str, str]] = set()
        root = self.code_root / "manifests" / "dependencies"
        manifests = sorted(root.glob("*/*.json")) if root.is_dir() else []
        if not manifests:
            errors.append(f"missing dependency manifests: {root}")
        for manifest in manifests:
            checked += 1
            try:
                value = json.loads(manifest.read_text(encoding="utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("manifest must be an object")
                library = str(value["library"])
                version = str(value["version"])
                commit = str(value["commit"])
                dependencies = value["dependencies"]
                if value.get("schema_name") != "dependency_manifest":
                    errors.append(f"{manifest} has an invalid schema_name")
                if value.get("schema_version") != "2.0":
                    errors.append(f"{manifest} has an invalid schema_version")
                identity = (library, version)
                if identity in seen:
                    errors.append(f"duplicate dependency manifest: {library} {version}")
                seen.add(identity)
                if manifest.parent.name != library or manifest.stem != version:
                    errors.append(f"dependency manifest path/identity mismatch: {manifest}")
                if not isinstance(dependencies, list):
                    raise ValueError("dependencies must be a list")
                if value.get("dependency_count") != len(dependencies):
                    errors.append(f"{manifest} dependency_count mismatch")
                if value.get("content_hash") != sha256_json(dependencies):
                    errors.append(f"{manifest} content_hash mismatch")
                marker = (
                    self.code_root
                    / "sources"
                    / "repositories"
                    / library
                    / version
                    / "current.json"
                )
                self._validate_dependency_marker(marker, commit, value, manifest, errors)
                for index, dependency in enumerate(dependencies, 1):
                    self._validate_dependency(dependency, manifest, index, errors)
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                errors.append(f"invalid dependency manifest {manifest}: {error}")
        return self._result("dependencies", checked, errors)

    def writing(self) -> dict[str, Any]:
        path = self.writing_root / "derived" / "writing_entries.jsonl"
        errors: list[str] = []
        checked = 0
        if path.is_file():
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                checked += 1
                try:
                    value = json.loads(line)
                    for field in ("writing_id", "source_paper_id", "writing_function", "abstract_pattern", "content_hash"):
                        if not value.get(field):
                            errors.append(f"{path}:{line_number} missing {field}")
                except json.JSONDecodeError:
                    errors.append(f"{path}:{line_number} invalid JSON")
        else:
            errors.append(f"missing writing manifest: {path}")
        return self._result("writing", checked, errors)

    def index(
        self,
        knowledge_base: str,
        *,
        qdrant_client: Any | None = None,
        collection: str | None = None,
    ) -> dict[str, Any]:
        if knowledge_base not in {"code", "writing"}:
            raise ValueError("index validation supports only code and writing")
        try:
            data_dir = self.rag_dirs[knowledge_base]
        except KeyError as exc:
            raise ValueError(f"missing RAG directory for {knowledge_base}") from exc
        errors: list[str] = []
        warnings: list[str] = []
        state_path = data_dir / "state" / "index.sqlite3"
        rows, tombstones = self._index_state(state_path, errors)
        source_records = self._source_records(knowledge_base, errors)
        local = self._chunk_artifacts(
            knowledge_base,
            data_dir / "chunks",
            rows,
            tombstones,
            source_records,
            errors,
        )
        qdrant = {
            "checked": False,
            "collection": collection,
            "points": None,
            "status": None,
        }
        if qdrant_client is None:
            warnings.append("qdrant_not_checked")
        elif not collection:
            errors.append("Qdrant collection is required for online index validation")
        else:
            qdrant = self._qdrant_index(
                qdrant_client,
                collection,
                knowledge_base,
                local["chunk_ids"],
                local["chunk_documents"],
                errors,
            )
        active_documents = sum(bool(row.get("active")) for row in rows.values())
        return {
            "check": "index",
            "knowledge_base": knowledge_base,
            "valid": not errors,
            "checked": {
                "state_documents": len(rows),
                "active_documents": active_documents,
                "tombstones": len(tombstones),
                "artifacts": local["artifacts"],
                "chunks": len(local["chunk_ids"]),
                "source_records": len(source_records),
            },
            "qdrant": qdrant,
            "warnings": warnings,
            "errors": errors[:200],
        }

    def all(
        self,
        *,
        indexes: Mapping[str, tuple[Any | None, str | None]] | None = None,
    ) -> dict[str, Any]:
        checks = [self.sources(), self.normalized(), self.dependencies(), self.writing()]
        for knowledge_base in ("code", "writing"):
            if knowledge_base not in self.rag_dirs:
                continue
            client, collection = (indexes or {}).get(knowledge_base, (None, None))
            checks.append(
                self.index(
                    knowledge_base,
                    qdrant_client=client,
                    collection=collection,
                )
            )
        return {"valid": all(item["valid"] for item in checks), "checks": checks}

    @staticmethod
    def _validate_dependency_marker(
        marker: Path,
        commit: str,
        manifest: Mapping[str, Any],
        path: Path,
        errors: list[str],
    ) -> None:
        if not marker.is_file():
            errors.append(f"{path} has no synchronized source marker: {marker}")
            return
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            errors.append(f"{path} has an unreadable source marker: {marker}")
            return
        if not isinstance(value, dict):
            errors.append(f"{path} has an invalid source marker: {marker}")
            return
        if len(commit) != 40 or value.get("commit") != commit:
            errors.append(f"{path} commit differs from synchronized source marker")
        if value.get("tag") != manifest.get("tag"):
            errors.append(f"{path} tag differs from synchronized source marker")
        if value.get("source_path") != manifest.get("source_path"):
            errors.append(f"{path} source_path differs from synchronized source marker")

    @staticmethod
    def _validate_dependency(
        dependency: object,
        path: Path,
        index: int,
        errors: list[str],
    ) -> None:
        prefix = f"{path} dependency {index}"
        if not isinstance(dependency, Mapping):
            errors.append(f"{prefix} must be an object")
            return
        for field in (
            "package",
            "normalized_package",
            "source",
            "evidence_kind",
            "relation",
            "scope",
        ):
            if not dependency.get(field):
                errors.append(f"{prefix} missing {field}")
        if dependency.get("inference") is not False:
            errors.append(f"{prefix} must be explicit evidence, not inference")
        confidence = dependency.get("confidence")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            errors.append(f"{prefix} has invalid confidence")
        elif not 0.0 <= float(confidence) <= 1.0:
            errors.append(f"{prefix} confidence is outside [0, 1]")

    @staticmethod
    def _index_state(
        path: Path, errors: list[str]
    ) -> tuple[dict[str, dict[str, Any]], set[str]]:
        if not path.is_file():
            errors.append(f"missing index state: {path}")
            return {}, set()
        rows: dict[str, dict[str, Any]] = {}
        tombstones: set[str] = set()
        try:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
                connection.row_factory = sqlite3.Row
                for row in connection.execute("SELECT * FROM documents"):
                    value = dict(row)
                    document_id = str(value.get("document_id") or "")
                    if not document_id:
                        errors.append(f"index state contains an empty document_id: {path}")
                        continue
                    rows[document_id] = value
                    for field in (
                        "content_hash",
                        "metadata_hash",
                        "embedding_fingerprint",
                    ):
                        digest = str(value.get(field) or "")
                        if len(digest) != 64:
                            errors.append(f"{path} {document_id} has invalid {field}")
                    if not value.get("processor_version"):
                        errors.append(f"{path} {document_id} missing processor_version")
                tombstones = {
                    str(row[0])
                    for row in connection.execute("SELECT document_id FROM tombstones")
                }
        except sqlite3.Error as error:
            errors.append(f"unreadable index state {path}: {error}")
            return {}, set()
        for document_id, row in rows.items():
            active = bool(row.get("active"))
            if active and document_id in tombstones:
                errors.append(f"active document is tombstoned: {document_id}")
            if not active and document_id not in tombstones:
                errors.append(f"inactive document lacks tombstone: {document_id}")
        unknown = tombstones - set(rows)
        errors.extend(f"tombstone has no state document: {value}" for value in sorted(unknown))
        return rows, tombstones

    def _source_records(
        self, knowledge_base: str, errors: list[str]
    ) -> dict[str, dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        if knowledge_base == "code":
            manifests = self.code_root.glob("normalized/**/*.jsonl")
            for manifest in manifests:
                if (
                    manifest.parent == self.code_root / "normalized"
                    and (manifest.parent / manifest.stem).is_dir()
                ):
                    continue
                self._load_source_manifest(manifest, "document_id", records, errors)
            return records
        path = self.writing_root / "derived" / "writing_entries.jsonl"
        if not path.is_file():
            errors.append(f"missing Writing source manifest: {path}")
            return records
        self._load_source_manifest(path, "writing_id", records, errors)
        return records

    @staticmethod
    def _load_source_manifest(
        path: Path,
        identity_field: str,
        records: dict[str, dict[str, Any]],
        errors: list[str],
    ) -> None:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            errors.append(f"unreadable source manifest {path}: {error}")
            return
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                identity = str(value[identity_field])
            except (KeyError, TypeError, json.JSONDecodeError):
                errors.append(f"{path}:{line_number} invalid source record")
                continue
            if identity in records:
                errors.append(f"duplicate source record: {identity}")
            records[identity] = value

    def _chunk_artifacts(
        self,
        knowledge_base: str,
        root: Path,
        rows: Mapping[str, Mapping[str, Any]],
        tombstones: set[str],
        source_records: Mapping[str, Mapping[str, Any]],
        errors: list[str],
    ) -> dict[str, Any]:
        if not root.is_dir():
            errors.append(f"missing chunk artifact directory: {root}")
            return {"artifacts": 0, "chunk_ids": set(), "chunk_documents": {}}
        expected_names = {
            f"{sha256_json(document_id)[:32]}.jsonl": document_id for document_id in rows
        }
        for artifact in root.glob("*.jsonl"):
            if artifact.name not in expected_names:
                errors.append(f"unreferenced chunk artifact: {artifact}")
        chunk_ids: set[str] = set()
        chunk_documents: dict[str, str] = {}
        artifact_count = 0
        for document_id, row in rows.items():
            if not bool(row.get("active")):
                continue
            artifact = root / f"{sha256_json(document_id)[:32]}.jsonl"
            if not artifact.is_file():
                errors.append(f"missing chunk artifact for {document_id}: {artifact}")
                continue
            artifact_count += 1
            values = self._read_chunks(artifact, errors)
            if not values:
                errors.append(f"empty chunk artifact for {document_id}: {artifact}")
                continue
            indexes: list[int] = []
            for line_number, value in values:
                chunk_id = str(value.get("chunk_id") or "")
                chunk_document = str(value.get("document_id") or "")
                metadata = value.get("metadata")
                if not chunk_id:
                    errors.append(f"{artifact}:{line_number} missing chunk_id")
                elif chunk_id in chunk_ids:
                    errors.append(f"duplicate chunk_id: {chunk_id}")
                else:
                    chunk_ids.add(chunk_id)
                    chunk_documents[chunk_id] = chunk_document
                if chunk_document != document_id:
                    errors.append(f"{artifact}:{line_number} document_id mismatch")
                index = value.get("chunk_index")
                if not isinstance(index, int) or isinstance(index, bool) or index < 0:
                    errors.append(f"{artifact}:{line_number} invalid chunk_index")
                else:
                    indexes.append(index)
                text = value.get("text")
                if not isinstance(text, str) or sha256_text(text) != value.get("text_sha256"):
                    errors.append(f"{artifact}:{line_number} text hash mismatch")
                if not value.get("chunk_fingerprint"):
                    errors.append(f"{artifact}:{line_number} missing chunk_fingerprint")
                self._validate_chunk_metadata(
                    knowledge_base,
                    document_id,
                    metadata,
                    source_records.get(document_id),
                    artifact,
                    line_number,
                    errors,
                )
            if sorted(indexes) != list(range(len(values))):
                errors.append(f"non-contiguous chunk indexes for {document_id}")
            self._validate_state_source(
                knowledge_base,
                document_id,
                row,
                source_records.get(document_id),
                values,
                errors,
            )
        active_without_source = {
            document_id
            for document_id, row in rows.items()
            if bool(row.get("active")) and document_id not in source_records
        }
        errors.extend(
            f"active index document has no source record: {value}"
            for value in sorted(active_without_source)
        )
        if tombstones - set(rows):
            errors.append("index contains orphan tombstones")
        return {
            "artifacts": artifact_count,
            "chunk_ids": chunk_ids,
            "chunk_documents": chunk_documents,
        }

    @staticmethod
    def _read_chunks(
        path: Path, errors: list[str]
    ) -> list[tuple[int, dict[str, Any]]]:
        values: list[tuple[int, dict[str, Any]]] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            errors.append(f"unreadable chunk artifact {path}: {error}")
            return values
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                errors.append(f"{path}:{line_number} invalid JSON")
                continue
            if not isinstance(value, dict):
                errors.append(f"{path}:{line_number} chunk must be an object")
                continue
            values.append((line_number, value))
        return values

    @staticmethod
    def _validate_chunk_metadata(
        knowledge_base: str,
        document_id: str,
        metadata: object,
        source: Mapping[str, Any] | None,
        path: Path,
        line_number: int,
        errors: list[str],
    ) -> None:
        if not isinstance(metadata, Mapping):
            errors.append(f"{path}:{line_number} metadata must be an object")
            return
        if metadata.get("knowledge_base") != knowledge_base:
            errors.append(f"{path}:{line_number} knowledge_base mismatch")
        required = (
            ("library", "version", "source_type", "source_url", "commit")
            if knowledge_base == "code"
            else ("source_paper_id", "writing_function", "source_type")
        )
        for field in required:
            if not metadata.get(field):
                errors.append(f"{path}:{line_number} missing metadata.{field}")
        if knowledge_base == "writing":
            writing_id = metadata.get("writing_id")
            if writing_id is not None and writing_id != document_id:
                errors.append(f"{path}:{line_number} writing_id mismatch")
            if source and metadata.get("source_paper_id") != source.get("source_paper_id"):
                errors.append(f"{path}:{line_number} source_paper_id mismatch")

    @staticmethod
    def _validate_state_source(
        knowledge_base: str,
        document_id: str,
        row: Mapping[str, Any],
        source: Mapping[str, Any] | None,
        chunks: list[tuple[int, dict[str, Any]]],
        errors: list[str],
    ) -> None:
        if source is None:
            return
        if knowledge_base == "code":
            if row.get("content_hash") != source.get("content_hash"):
                errors.append(f"state content_hash differs from normalized source: {document_id}")
            metadata = source.get("metadata")
            if not isinstance(metadata, Mapping) or row.get("metadata_hash") != sha256_json(
                metadata
            ):
                errors.append(f"state metadata_hash differs from normalized source: {document_id}")
            return
        first = chunks[0][1]
        metadata = first.get("metadata")
        text = first.get("text")
        if isinstance(text, str) and row.get("content_hash") != sha256_text(text):
            errors.append(f"state content_hash differs from Writing artifact: {document_id}")
        if not isinstance(metadata, Mapping) or row.get("metadata_hash") != sha256_json(metadata):
            errors.append(f"state metadata_hash differs from Writing artifact: {document_id}")

    @staticmethod
    def _qdrant_index(
        client: Any,
        collection: str,
        knowledge_base: str,
        local_chunk_ids: set[str],
        local_documents: Mapping[str, str],
        errors: list[str],
    ) -> dict[str, Any]:
        try:
            info = client.get_collection(collection)
            status = str(getattr(info.status, "value", info.status))
            points = int(info.points_count or 0)
            exact_points = int(client.count(collection_name=collection, exact=True).count)
        except Exception as error:
            errors.append(f"cannot inspect Qdrant collection {collection}: {error}")
            return {
                "checked": True,
                "collection": collection,
                "points": None,
                "status": "unavailable",
            }
        if status != "green":
            errors.append(f"Qdrant collection is not green: {collection} ({status})")
        if points != exact_points:
            errors.append(
                f"Qdrant reported point counts disagree: {points} info vs {exact_points} exact"
            )
        if exact_points != len(local_chunk_ids):
            errors.append(
                f"Qdrant/local chunk count mismatch: {exact_points} vs {len(local_chunk_ids)}"
            )
        remote_ids: set[str] = set()
        offset: Any = None
        try:
            while True:
                page, offset = client.scroll(
                    collection_name=collection,
                    limit=256,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                for point in page:
                    point_id = str(point.id)
                    payload = point.payload or {}
                    remote_ids.add(point_id)
                    if payload.get("chunk_id") != point_id:
                        errors.append(f"Qdrant point/chunk_id mismatch: {point_id}")
                    expected_document = local_documents.get(point_id)
                    if payload.get("document_id") != expected_document:
                        errors.append(f"Qdrant document_id mismatch: {point_id}")
                    if payload.get("knowledge_base") != knowledge_base:
                        errors.append(f"Qdrant knowledge_base mismatch: {point_id}")
                if offset is None:
                    break
        except Exception as error:
            errors.append(f"cannot scroll Qdrant collection {collection}: {error}")
        missing = local_chunk_ids - remote_ids
        extra = remote_ids - local_chunk_ids
        errors.extend(f"Qdrant missing chunk: {value}" for value in sorted(missing))
        errors.extend(f"Qdrant has unknown chunk: {value}" for value in sorted(extra))
        return {
            "checked": True,
            "collection": collection,
            "points": exact_points,
            "status": status,
        }

    @staticmethod
    def _result(name: str, checked: int, errors: list[str]) -> dict[str, Any]:
        return {"check": name, "valid": not errors, "checked": checked, "errors": errors[:200]}
