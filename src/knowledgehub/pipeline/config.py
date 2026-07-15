"""Configuration and GPU planning for the unified RAG pipeline."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field, fields, replace
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

import yaml

EMBEDDING_REVISION = "5cf2132abc99cad020ac570b19d031efec650f2b"
LIGHT_RERANKER_REVISION = "e61197ed45024b0ed8a2d74b80b4d909f1255473"
QUALITY_RERANKER_REVISION = "22e683669bc0f0bd69640a1354a6d0aebcfeede5"
QUERY_INSTRUCTION = (
    "Given a research question, retrieve relevant passages from academic papers "
    "that answer the question."
)


class RagConfigError(ValueError):
    """A sanitized configuration failure."""


class SecretValue:
    __slots__ = ("_value",)

    def __init__(self, value: str = "") -> None:
        self._value = value

    def get_secret_value(self) -> str:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __repr__(self) -> str:
        return "SecretValue('********')"

    __str__ = __repr__


class GPUMode(str, Enum):
    AUTO = "auto"
    DUAL = "dual"
    SINGLE = "single"
    CPU = "cpu"


class RerankerProfile(str, Enum):
    OFF = "off"
    LIGHT = "light"
    QUALITY = "quality"


@dataclass(frozen=True, slots=True)
class GPUDevice:
    logical_id: int
    physical_id: str
    name: str
    total_memory_mb: int
    free_memory_mb: int
    uuid: str
    pci_bus_id: str


@dataclass(frozen=True, slots=True)
class GPUPlan:
    requested_mode: str
    resolved_mode: str
    gpu_ids: tuple[int, ...]
    devices: tuple[GPUDevice, ...]
    parser_workers: int
    embedding_endpoints: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_mode": self.requested_mode,
            "resolved_mode": self.resolved_mode,
            "gpu_ids": list(self.gpu_ids),
            "parser_workers": self.parser_workers,
            "embedding_endpoints": list(self.embedding_endpoints),
            "devices": [
                {
                    "logical_id": value.logical_id,
                    "physical_id": value.physical_id,
                    "name": value.name,
                    "total_memory_mb": value.total_memory_mb,
                    "free_memory_mb": value.free_memory_mb,
                    "uuid": value.uuid,
                    "pci_bus_id": value.pci_bus_id,
                }
                for value in self.devices
            ],
        }


@dataclass(frozen=True)
class RagConfig:
    source: str = "zotero"
    source_snapshot_path: Path = Path("/data/KnowledgeHub/zotero/manifests/documents.jsonl")
    source_delta_catalog_path: Path = Path(
        "/data/KnowledgeHub/zotero/manifests/delta-catalog.jsonl"
    )
    data_dir: Path = Path("/data/KnowledgeHub/rag/zotero")
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_collection: str = "zotero_papers_qwen3_4b_1024_v2"
    qdrant_smoke_collection: str = "zotero_papers_qwen3_4b_1024_smoke"
    qdrant_upsert_batch_size: int = 32
    model_cache_dir: Path = Path("/data/KnowledgeHub/model-cache")
    gpu_mode: str = GPUMode.AUTO.value
    gpu_ids: tuple[int, ...] = (0, 1)
    parse_device: str = "cuda"
    parse_gpu_ids: tuple[int, ...] = (0, 1)
    parse_workers_per_gpu: int = 1
    parse_cpu_threads_per_worker: int = 8
    parser_name: str = "docling"
    parser_fallback: str = "pymupdf"
    ocr_enabled: bool = False
    chunk_max_tokens: int = 768
    chunk_merge_peers: bool = True
    embedding_model: str = "Qwen/Qwen3-Embedding-4B"
    embedding_revision: str = EMBEDDING_REVISION
    embedding_dtype: str = "float16"
    embedding_dim: int = 1024
    embedding_normalize: bool = True
    embedding_max_length: int = 8192
    embedding_batch_size: int = 16
    embedding_max_batch_tokens: int = 8192
    embedding_query_instruction: str = QUERY_INSTRUCTION
    embedding_endpoints: tuple[str, ...] = (
        "http://127.0.0.1:8080",
        "http://127.0.0.1:8082",
    )
    embedding_request_strategy: str = "least_outstanding"
    embedding_timeout_seconds: float = 120.0
    embedding_api_key: SecretValue = field(default_factory=SecretValue)
    reranker_profile: str = RerankerProfile.OFF.value
    reranker_url: str = "http://127.0.0.1:8081"
    reranker_gpu_id: int = 1
    reranker_max_length: int = 2048
    reranker_batch_size: int = 4
    search_api_key: SecretValue = field(default_factory=SecretValue)
    reranker_api_key: SecretValue = field(default_factory=SecretValue)
    sparse_model: str = "Qdrant/bm25"
    cuda_allow_tf32: bool = True
    gpu_memory_safety_margin_mb: int = 2048
    log_level: str = "INFO"

    @classmethod
    def load(
        cls,
        config_path: Path | str | None = None,
        *,
        profile_path: Path | str | None = None,
        environ: Mapping[str, str] | None = None,
        overrides: Mapping[str, Any] | None = None,
    ) -> "RagConfig":
        values: dict[str, Any] = {}
        for candidate in (config_path, profile_path):
            if candidate is not None:
                values.update(_read_yaml(Path(candidate)))
        env = os.environ if environ is None else environ
        for env_name, field_name in _ENV_FIELDS.items():
            if env_name in env:
                values[field_name] = env[env_name]
        if overrides:
            values.update({key: value for key, value in overrides.items() if value is not None})
        allowed = {value.name for value in fields(cls)}
        unknown = sorted(set(values) - allowed)
        if unknown:
            raise RagConfigError(f"unknown RAG configuration keys: {', '.join(unknown)}")
        converted = {name: _convert(name, value) for name, value in values.items()}
        return cls(**converted).validate()

    def validate(self) -> "RagConfig":
        try:
            GPUMode(self.gpu_mode)
            RerankerProfile(self.reranker_profile)
        except ValueError as exc:
            raise RagConfigError(str(exc)) from exc
        if self.source != "zotero":
            raise RagConfigError("only the zotero source is implemented")
        if len(set(self.gpu_ids)) != len(self.gpu_ids) or any(value < 0 for value in self.gpu_ids):
            raise RagConfigError("GPU IDs must be unique non-negative integers")
        if len(set(self.parse_gpu_ids)) != len(self.parse_gpu_ids):
            raise RagConfigError("parse GPU IDs must be unique")
        if self.gpu_mode == GPUMode.DUAL.value and len(self.gpu_ids) != 2:
            raise RagConfigError("dual mode requires exactly two GPU IDs")
        if self.gpu_mode == GPUMode.SINGLE.value and len(self.gpu_ids) != 1:
            raise RagConfigError("single mode requires exactly one GPU ID")
        if self.parse_workers_per_gpu != 1:
            raise RagConfigError("v1 supports exactly one parser worker per GPU")
        if self.chunk_max_tokens <= 0 or self.embedding_dim <= 0:
            raise RagConfigError("chunk and embedding dimensions must be positive")
        if self.qdrant_upsert_batch_size <= 0:
            raise RagConfigError("Qdrant upsert batch size must be positive")
        if self.embedding_dim > 2560:
            raise RagConfigError("embedding_dim exceeds Qwen3-Embedding-4B output dimension")
        if self.embedding_request_strategy not in {"round_robin", "least_outstanding"}:
            raise RagConfigError("invalid embedding request strategy")
        if not self.embedding_revision or len(self.embedding_revision) != 40:
            raise RagConfigError("embedding revision must be a full commit SHA")
        if not self.embedding_endpoints:
            raise RagConfigError("at least one embedding endpoint is required")
        if self.reranker_batch_size <= 0 or self.reranker_max_length <= 0:
            raise RagConfigError("reranker batch size and max length must be positive")
        return self

    def with_overrides(self, **values: Any) -> "RagConfig":
        return replace(self, **values).validate()

    def prepare_runtime(self) -> None:
        source_root = self.source_snapshot_path.expanduser().resolve(strict=False).parent.parent
        data = self.data_dir.expanduser().resolve(strict=False)
        if data == source_root or data in source_root.parents or source_root in data.parents:
            raise RagConfigError("RAG data directory must not overlap the source data directory")
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for relative in (
            "state",
            "parsed/json",
            "parsed/markdown",
            "chunks",
            "build/benchmarks",
            "runs",
            "failures",
            "snapshots",
            "logs",
            ".staging",
        ):
            path = self.data_dir / relative
            if path.is_symlink():
                raise RagConfigError(f"runtime path must not be a symlink: {path}")
            path.mkdir(parents=True, exist_ok=True, mode=0o700)

    def resolve_gpu_plan(self, devices: tuple[GPUDevice, ...] | None = None) -> GPUPlan:
        available = devices if devices is not None else inspect_gpu_devices()
        by_id = {device.logical_id: device for device in available}
        requested = GPUMode(self.gpu_mode)
        if requested is GPUMode.AUTO:
            resolved = (
                GPUMode.DUAL
                if len(available) >= 2
                else GPUMode.SINGLE
                if available
                else GPUMode.CPU
            )
        else:
            resolved = requested
        ids: tuple[int, ...]
        if resolved is GPUMode.CPU:
            ids = ()
        else:
            count = 2 if resolved is GPUMode.DUAL else 1
            ids = self.gpu_ids[:count]
            missing = [value for value in ids if value not in by_id]
            if missing:
                raise RagConfigError(f"requested GPU IDs are unavailable: {missing}")
        endpoints = self.embedding_endpoints[: max(1, len(ids))]
        return GPUPlan(
            requested_mode=requested.value,
            resolved_mode=resolved.value,
            gpu_ids=ids,
            devices=tuple(by_id[value] for value in ids),
            parser_workers=max(1, len(ids)),
            embedding_endpoints=endpoints,
        )


def inspect_gpu_devices() -> tuple[GPUDevice, ...]:
    """Inspect NVIDIA devices without importing torch or initializing CUDA."""

    executable = shutil.which("nvidia-smi")
    if executable is None:
        return ()
    query = "index,name,memory.total,memory.free,uuid,pci.bus_id"
    completed = subprocess.run(
        [executable, f"--query-gpu={query}", "--format=csv,noheader,nounits"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if completed.returncode != 0:
        return ()
    result: list[GPUDevice] = []
    for line in completed.stdout.splitlines():
        parts = [value.strip() for value in line.split(",")]
        if len(parts) != 6:
            continue
        result.append(
            GPUDevice(
                logical_id=int(parts[0]),
                physical_id=parts[0],
                name=parts[1],
                total_memory_mb=int(parts[2]),
                free_memory_mb=int(parts[3]),
                uuid=parts[4],
                pci_bus_id=parts[5],
            )
        )
    return tuple(result)


_ENV_FIELDS = {
    "KH_ZOTERO_SNAPSHOT_PATH": "source_snapshot_path",
    "KH_ZOTERO_DELTA_CATALOG_PATH": "source_delta_catalog_path",
    "KH_RAG_DATA_DIR": "data_dir",
    "KH_QDRANT_URL": "qdrant_url",
    "KH_QDRANT_COLLECTION": "qdrant_collection",
    "KH_QDRANT_SMOKE_COLLECTION": "qdrant_smoke_collection",
    "KH_QDRANT_UPSERT_BATCH_SIZE": "qdrant_upsert_batch_size",
    "KH_MODEL_CACHE_DIR": "model_cache_dir",
    "KH_GPU_MODE": "gpu_mode",
    "KH_GPU_IDS": "gpu_ids",
    "KH_PARSE_DEVICE": "parse_device",
    "KH_PARSE_GPU_IDS": "parse_gpu_ids",
    "KH_PARSE_WORKERS_PER_GPU": "parse_workers_per_gpu",
    "KH_PARSE_CPU_THREADS_PER_WORKER": "parse_cpu_threads_per_worker",
    "KH_PARSER_NAME": "parser_name",
    "KH_PARSER_FALLBACK": "parser_fallback",
    "KH_OCR_ENABLED": "ocr_enabled",
    "KH_CHUNK_MAX_TOKENS": "chunk_max_tokens",
    "KH_CHUNK_MERGE_PEERS": "chunk_merge_peers",
    "KH_EMBEDDING_MODEL": "embedding_model",
    "KH_EMBEDDING_REVISION": "embedding_revision",
    "KH_EMBEDDING_DTYPE": "embedding_dtype",
    "KH_EMBEDDING_DIM": "embedding_dim",
    "KH_EMBEDDING_NORMALIZE": "embedding_normalize",
    "KH_EMBEDDING_MAX_LENGTH": "embedding_max_length",
    "KH_EMBEDDING_BATCH_SIZE": "embedding_batch_size",
    "KH_EMBEDDING_MAX_BATCH_TOKENS": "embedding_max_batch_tokens",
    "KH_EMBEDDING_QUERY_INSTRUCTION": "embedding_query_instruction",
    "KH_EMBED_ENDPOINTS": "embedding_endpoints",
    "KH_EMBED_REQUEST_STRATEGY": "embedding_request_strategy",
    "KH_EMBEDDING_TIMEOUT_SECONDS": "embedding_timeout_seconds",
    "KH_EMBEDDING_API_KEY": "embedding_api_key",
    "KH_RERANKER_PROFILE": "reranker_profile",
    "KH_RERANKER_URL": "reranker_url",
    "KH_RERANK_GPU_ID": "reranker_gpu_id",
    "KH_RERANKER_MAX_LENGTH": "reranker_max_length",
    "KH_RERANKER_BATCH_SIZE": "reranker_batch_size",
    "KH_SEARCH_API_KEY": "search_api_key",
    "KH_RERANKER_API_KEY": "reranker_api_key",
    "KH_SPARSE_MODEL": "sparse_model",
    "KH_CUDA_ALLOW_TF32": "cuda_allow_tf32",
    "KH_GPU_MEMORY_SAFETY_MARGIN_MB": "gpu_memory_safety_margin_mb",
    "KH_RAG_LOG_LEVEL": "log_level",
}

_PATH_FIELDS = {
    "source_snapshot_path",
    "source_delta_catalog_path",
    "data_dir",
    "model_cache_dir",
}
_TUPLE_INT_FIELDS = {"gpu_ids", "parse_gpu_ids"}
_TUPLE_STRING_FIELDS = {"embedding_endpoints"}
_INT_FIELDS = {
    "parse_workers_per_gpu",
    "parse_cpu_threads_per_worker",
    "chunk_max_tokens",
    "embedding_dim",
    "embedding_max_length",
    "embedding_batch_size",
    "embedding_max_batch_tokens",
    "qdrant_upsert_batch_size",
    "reranker_gpu_id",
    "reranker_max_length",
    "reranker_batch_size",
    "gpu_memory_safety_margin_mb",
}
_BOOL_FIELDS = {"ocr_enabled", "chunk_merge_peers", "embedding_normalize", "cuda_allow_tf32"}


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RagConfigError(f"RAG configuration file does not exist: {path}")
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, Mapping):
        raise RagConfigError(f"RAG configuration root must be a mapping: {path}")
    value: Any = loaded.get("rag", loaded)
    if not isinstance(value, Mapping):
        raise RagConfigError(f"RAG configuration must be a mapping: {path}")
    return {str(key): item for key, item in value.items()}


def _convert(name: str, value: Any) -> Any:
    if name in {"embedding_api_key", "search_api_key", "reranker_api_key"}:
        return value if isinstance(value, SecretValue) else SecretValue(str(value).strip())
    if name in _PATH_FIELDS:
        return Path(str(value)).expanduser()
    if name in _TUPLE_INT_FIELDS:
        raw = value if isinstance(value, (list, tuple)) else str(value).split(",")
        return tuple(int(item) for item in raw if str(item).strip())
    if name in _TUPLE_STRING_FIELDS:
        raw = value if isinstance(value, (list, tuple)) else str(value).split(",")
        return tuple(str(item).strip().rstrip("/") for item in raw if str(item).strip())
    if name in _INT_FIELDS:
        return int(value)
    if name == "embedding_timeout_seconds":
        return float(value)
    if name in _BOOL_FIELDS:
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        raise RagConfigError(f"{name} must be boolean")
    return str(value).strip()
