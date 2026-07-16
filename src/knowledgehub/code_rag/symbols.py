"""Python AST symbol catalog and deterministic intra-repository relations."""

from __future__ import annotations

import ast
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from knowledgehub.core.hashing import sha256_json, sha256_text


@dataclass(frozen=True, slots=True)
class SymbolRecord:
    symbol_id: str
    library: str
    version: str
    module: str
    qualified_name: str
    symbol_type: str
    path: str
    start_line: int
    end_line: int
    signature: str
    docstring_hash: str
    ast_hash: str


class SymbolIndex:
    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self.path = path
        self.read_only = read_only
        if read_only:
            if not path.is_file():
                raise FileNotFoundError(path)
            return
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with sqlite3.connect(path) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS symbols(
                  symbol_id TEXT PRIMARY KEY,library TEXT,version TEXT,module TEXT,
                  qualified_name TEXT,symbol_type TEXT,path TEXT,start_line INTEGER,end_line INTEGER,
                  signature TEXT,docstring_hash TEXT,ast_hash TEXT);
                CREATE INDEX IF NOT EXISTS symbol_lookup ON symbols(library,version,qualified_name);
                CREATE TABLE IF NOT EXISTS relations(
                  library TEXT,version TEXT,source_symbol TEXT,relation TEXT,target_symbol TEXT,
                  evidence TEXT,PRIMARY KEY(library,version,source_symbol,relation,target_symbol));
                """
            )

    def build(self, library: str, version: str, root: Path, paths: Iterable[Path]) -> dict[str, int]:
        symbols: dict[str, SymbolRecord] = {}
        relations: set[tuple[str, str, str, str]] = set()
        for path in paths:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(text)
            except (OSError, SyntaxError):
                continue
            relative = path.relative_to(root).as_posix()
            module = relative.removesuffix(".py").replace("/", ".")
            for node, parent in _nodes(tree):
                record = _symbol(library, version, module, relative, node, parent)
                if record:
                    # Conditional imports/backends sometimes define the same
                    # qualified symbol more than once. Sorted path traversal
                    # plus AST body order makes the final definition stable.
                    symbols[record.symbol_id] = record
                    for relation, target in _relations(node):
                        relations.add((record.qualified_name, relation, target, relative))
        with sqlite3.connect(self.path) as connection:
            connection.execute("DELETE FROM relations WHERE library=? AND version=?", (library, version))
            connection.execute("DELETE FROM symbols WHERE library=? AND version=?", (library, version))
            connection.executemany(
                "INSERT INTO symbols VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        record.symbol_id, record.library, record.version, record.module,
                        record.qualified_name, record.symbol_type, record.path,
                        record.start_line, record.end_line, record.signature,
                        record.docstring_hash, record.ast_hash,
                    )
                    for record in symbols.values()
                ],
            )
            connection.executemany(
                "INSERT OR IGNORE INTO relations VALUES(?,?,?,?,?,?)",
                [(library, version, source, relation, target, evidence) for source, relation, target, evidence in relations],
            )
        return {"symbols": len(symbols), "relations": len(relations)}

    def inspect(self, library: str, version: str, symbol: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM symbols WHERE library=? AND version=? AND (qualified_name=? OR qualified_name LIKE ?) ORDER BY length(qualified_name) LIMIT 1",
                (library, version, symbol, f"%.{symbol}"),
            ).fetchone()
            if not row:
                return None
            relations = connection.execute(
                "SELECT relation,target_symbol,evidence FROM relations WHERE library=? AND version=? AND source_symbol=?",
                (library, version, row["qualified_name"]),
            ).fetchall()
        relation_values = [dict(value) for value in relations]
        return {
            **dict(row),
            "relation_count": len(relation_values),
            "relations": relation_values[:200],
            "relations_truncated": len(relation_values) > 200,
        }

    def versions(self, library: str, symbol: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM symbols WHERE library=? AND (qualified_name=? OR qualified_name LIKE ?) ORDER BY version",
                (library, symbol, f"%.{symbol}"),
            ).fetchall()
        return [dict(row) for row in rows]

    def _connection(self) -> sqlite3.Connection:
        if self.read_only:
            return sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        return sqlite3.connect(self.path)


def _nodes(tree: ast.AST) -> Iterable[tuple[ast.AST, str | None]]:
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node, None
            if isinstance(node, ast.ClassDef):
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        yield child, node.name
        elif isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign)):
            yield node, None


def _symbol(library: str, version: str, module: str, path: str, node: ast.AST, parent: str | None) -> SymbolRecord | None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        name = node.name
        kind = "class" if isinstance(node, ast.ClassDef) else "method" if parent else "function"
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(isinstance(item, ast.Name) and item.id == "property" for item in node.decorator_list):
            kind = "property"
        signature = _signature(node)
        docstring = ast.get_docstring(node) or ""
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        name, kind, signature, docstring = f"import@{node.lineno}", "import", ast.unparse(node), ""
    elif isinstance(node, (ast.Assign, ast.AnnAssign)):
        target = node.targets[0] if isinstance(node, ast.Assign) else node.target
        if not isinstance(target, ast.Name) or not target.id.isupper():
            return None
        name, kind, signature, docstring = target.id, "constant", ast.unparse(node), ""
    else:
        return None
    qualified = ".".join(value for value in (module, parent, name) if value)
    return SymbolRecord(
        f"{library}@{version}::{module}::{'.'.join(value for value in (parent, name) if value)}",
        library, version, module, qualified, kind, path, int(getattr(node, "lineno", 1)),
        int(getattr(node, "end_lineno", getattr(node, "lineno", 1))), signature,
        sha256_text(docstring), sha256_json(ast.dump(node, include_attributes=False)),
    )


def _signature(node: ast.AST) -> str:
    if isinstance(node, ast.ClassDef):
        return f"class {node.name}({', '.join(ast.unparse(base) for base in node.bases)})"
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return f"{node.name}{ast.unparse(node.args)}" + (f" -> {ast.unparse(node.returns)}" if node.returns else "")
    return ""


def _relations(node: ast.AST) -> Iterable[tuple[str, str]]:
    if isinstance(node, ast.ClassDef):
        for base in node.bases:
            yield "inherits_from", ast.unparse(base)
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            yield "calls", ast.unparse(child.func)
        elif isinstance(child, ast.Import):
            for alias in child.names:
                yield "imports", alias.name
        elif isinstance(child, ast.ImportFrom):
            yield "imports", str(child.module or "")
