"""Structure-aware chunking for source code, documentation and releases."""

from __future__ import annotations

import ast
import re
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

from knowledgehub.core.hashing import sha256_json, sha256_text
from knowledgehub.core.models import KnowledgeDocument
from knowledgehub.pipeline.models import ChunkRecord

_NAMESPACE = uuid.UUID("79cbf091-1a02-51e9-885d-3205873df04b")
_MARKDOWN_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_RST_UNDERLINE = re.compile(r"^[=\-~^\"'`:+*#]{3,}\s*$")


def source_type_for_path(path: str) -> str:
    lowered = path.lower()
    name = Path(path).name.lower()
    if "migration" in lowered:
        return "migration_guide"
    if "changelog" in name or "release" in name:
        return "changelog"
    if lowered.endswith(".py"):
        return "source_code"
    if "/examples/" in f"/{lowered}" or lowered.startswith("examples/"):
        return "example"
    if "/tutorial" in f"/{lowered}":
        return "tutorial"
    if name.startswith("readme"):
        return "repository_readme"
    return "api_documentation"


def task_tags(source_type: str, text: str) -> tuple[str, ...]:
    result: set[str] = set()
    lowered = text.lower()
    mapping = {
        "compatibility": ("compatib", "breaking", "version"),
        "migration": ("migrat", "upgrade"),
        "deprecation": ("deprecat",),
        "debug": ("error", "exception", "bug", "fix"),
        "installation": ("install",),
        "configuration": ("config",),
        "performance": ("performance", "memory", "speed"),
    }
    for tag, words in mapping.items():
        if any(word in lowered for word in words):
            result.add(tag)
    if source_type in {"example", "tutorial"}:
        result.add("example")
        result.add("api_usage")
    if source_type == "source_code":
        result.add("implementation")
    return tuple(sorted(result))


class CodeChunker:
    version = "code-structural-v1"

    def chunk(self, document: KnowledgeDocument) -> tuple[ChunkRecord, ...]:
        content = document.read_content()
        path = str(document.metadata.get("path") or "")
        if document.source_type == "source_code" and path.endswith(".py"):
            rows = self._python(content, path)
        elif document.source_type == "release_note":
            rows = self._release(content)
        else:
            rows = self._markup(content)
        chunks: list[ChunkRecord] = []
        for index, (text, extra) in enumerate(rows):
            if not text.strip():
                continue
            metadata = {**dict(document.metadata), **extra}
            metadata.update(
                {
                    "knowledge_base": "code",
                    "source_type": document.source_type,
                    "source_url": document.source_url,
                    "title": document.title,
                    "task_tags": list(task_tags(document.source_type, text)),
                }
            )
            fingerprint = sha256_json(
                {
                    "document_id": document.document_id,
                    "index": index,
                    "processor": self.version,
                    "text": text,
                    "metadata": metadata,
                }
            )
            chunk_id = str(uuid.uuid5(_NAMESPACE, f"{document.document_id}\0{index}\0{fingerprint}"))
            chunks.append(
                ChunkRecord(
                    chunk_id=chunk_id,
                    document_id=document.document_id,
                    attachment_key="",
                    chunk_index=index,
                    text=text.strip(),
                    text_sha256=sha256_text(text.strip()),
                    chunk_fingerprint=fingerprint,
                    token_count=max(1, len(text.split())),
                    page_start=None,
                    page_end=None,
                    section_path=tuple(str(extra.get("section") or "").split(" > ")) if extra.get("section") else (),
                    metadata=metadata,
                )
            )
        return tuple(chunks)

    def _python(self, content: str, path: str) -> Iterable[tuple[str, Mapping[str, Any]]]:
        try:
            tree = ast.parse(content)
        except SyntaxError:
            yield from self._fixed(content, {"path": path, "symbol_type": "module"})
            return
        lines = content.splitlines()
        imports = [
            "\n".join(lines[node.lineno - 1 : int(node.end_lineno or node.lineno)])
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        module = path.removesuffix(".py").replace("/", ".")
        prefix = "\n".join(imports[:30])
        yielded = False
        for node in tree.body:
            if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            yielded = True
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            symbol = f"{module}.{node.name}" if module else node.name
            text = "\n".join(lines[node.lineno - 1 : int(node.end_lineno or node.lineno)])
            if isinstance(node, ast.ClassDef):
                methods = [
                    child
                    for child in node.body
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                ]
                if methods:
                    # Index the class declaration, docstring and attributes as
                    # class context; method bodies are emitted separately.
                    text = "\n".join(lines[node.lineno - 1 : methods[0].lineno - 1])
            body = f"{prefix}\n\n{text}" if prefix else text
            yield from self._fixed(
                body,
                {
                    "module": module,
                    "symbol": symbol,
                    "symbol_type": kind,
                    "parent_symbol": None,
                    "start_line": node.lineno,
                    "end_line": int(node.end_lineno or node.lineno),
                },
            )
            if isinstance(node, ast.ClassDef):
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        method = "\n".join(
                            lines[child.lineno - 1 : int(child.end_lineno or child.lineno)]
                        )
                        yield from self._fixed(
                            f"{prefix}\n\nclass {node.name}:\n{method}" if prefix else f"class {node.name}:\n{method}",
                            {
                                "module": module,
                                "symbol": f"{symbol}.{child.name}",
                                "symbol_type": "method",
                                "parent_symbol": symbol,
                                "start_line": child.lineno,
                                "end_line": int(child.end_lineno or child.lineno),
                            },
                        )
        if not yielded:
            yield from self._fixed(content, {"module": module, "symbol_type": "module"})

    def _markup(self, content: str) -> Iterable[tuple[str, Mapping[str, Any]]]:
        lines = content.splitlines()
        sections: list[str] = []
        current: list[str] = []
        current_section = ""
        in_fence = False
        for index, line in enumerate(lines):
            if line.strip().startswith("```"):
                in_fence = not in_fence
            heading = _MARKDOWN_HEADING.match(line) if not in_fence else None
            rst_heading = (
                index + 1 < len(lines) and _RST_UNDERLINE.match(lines[index + 1])
                if not in_fence
                else None
            )
            if heading or rst_heading:
                if current:
                    yield from self._fixed("\n".join(current), {"section": current_section})
                title = heading.group(2).strip() if heading else line.strip()
                level = len(heading.group(1)) if heading else 2
                sections[:] = sections[: max(0, level - 1)]
                sections.append(title)
                current_section = " > ".join(sections)
                current = [line]
            else:
                current.append(line)
        if current:
            yield from self._fixed("\n".join(current), {"section": current_section})

    def _release(self, content: str) -> Iterable[tuple[str, Mapping[str, Any]]]:
        categories = {
            "breaking": "breaking_changes",
            "deprecat": "deprecations",
            "security": "security",
            "migrat": "migration",
            "performance": "performance",
            "bug": "bug_fixes",
            "fix": "bug_fixes",
            "known issue": "known_issues",
        }
        for text, metadata in self._markup(content):
            lowered = text.lower()
            category = next((value for key, value in categories.items() if key in lowered), "new_features")
            yield text, {**metadata, "change_category": category}

    @staticmethod
    def _fixed(
        text: str, metadata: Mapping[str, Any], *, max_chars: int = 8000
    ) -> Iterable[tuple[str, Mapping[str, Any]]]:
        value = text.strip()
        if not value:
            return
        if len(value) <= max_chars:
            yield value, metadata
            return
        lines = value.splitlines()
        start = 0
        current: list[str] = []
        size = 0
        for offset, line in enumerate(lines):
            if current and size + len(line) + 1 > max_chars:
                yield "\n".join(current), {**metadata, "part_start_line": start + 1, "part_end_line": offset}
                current, size, start = [], 0, offset
            current.append(line)
            size += len(line) + 1
        if current:
            yield "\n".join(current), {**metadata, "part_start_line": start + 1, "part_end_line": len(lines)}
