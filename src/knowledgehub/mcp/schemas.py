"""Strict MCP tool input schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchFilters(StrictModel):
    collection: str | None = Field(default=None, max_length=128)
    tag: str | None = Field(default=None, max_length=128)
    year_from: int | None = Field(default=None, ge=1400, le=2200)
    year_to: int | None = Field(default=None, ge=1400, le=2200)
    doi: str | None = Field(default=None, max_length=256)
    document_id: str | None = Field(default=None, max_length=256)
    attachment_key: str | None = Field(default=None, max_length=64)
    source: str | None = Field(default="zotero", max_length=128)
    library: str | None = Field(default=None, max_length=128)
    package: str | None = Field(default=None, max_length=128)
    version: str | None = Field(default=None, max_length=64)
    installed_version: str | None = Field(default=None, max_length=64)
    target_version: str | None = Field(default=None, max_length=64)
    source_types: tuple[str, ...] = Field(default=(), max_length=16)
    repository: str | None = Field(default=None, max_length=256)
    path: str | None = Field(default=None, max_length=1000)
    symbol: str | None = Field(default=None, max_length=512)
    section: str | None = Field(default=None, max_length=256)
    writing_function: str | None = Field(default=None, max_length=128)
    research_domain: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def valid_years(self) -> "SearchFilters":
        if self.year_from and self.year_to and self.year_from > self.year_to:
            raise ValueError("year_from cannot exceed year_to")
        return self


class NeighborExpansion(StrictModel):
    before: int = Field(default=0, ge=0, le=5)
    after: int = Field(default=0, ge=0, le=5)


class SearchInput(StrictModel):
    knowledge_base: Literal["literature", "code", "writing"] = "literature"
    query: str = Field(min_length=1, max_length=4000)
    intent: str | None = Field(default=None, max_length=64)
    return_mode: Literal["pattern_first", "include_original"] = "pattern_first"
    mode: Literal["dense", "sparse", "hybrid"] = "hybrid"
    limit: int = Field(default=10, ge=1, le=50)
    prefetch_limit: int = Field(default=50, ge=1, le=200)
    filters: SearchFilters = Field(default_factory=SearchFilters)
    fallback: Literal["strict", "degrade"] = "degrade"
    reranker: Literal["off", "auto", "light", "quality"] = "off"
    neighbors: NeighborExpansion = Field(default_factory=NeighborExpansion)
    max_chars_per_hit: int = Field(default=8000, ge=256, le=20000)

    @model_validator(mode="after")
    def valid_prefetch(self) -> "SearchInput":
        if self.prefetch_limit < self.limit:
            raise ValueError("prefetch_limit cannot be smaller than limit")
        return self


class GetChunkInput(StrictModel):
    chunk_id: str = Field(min_length=1, max_length=256)
    max_chars: int = Field(default=20000, ge=256, le=120000)


class GetDocumentInput(StrictModel):
    document_id: str = Field(min_length=1, max_length=256)
    include_abstract: bool = True
    chunk_cursor: int = Field(default=0, ge=0)
    chunk_limit: int = Field(default=100, ge=1, le=500)


class GetNeighborsInput(StrictModel):
    chunk_id: str = Field(min_length=1, max_length=256)
    before: int = Field(default=2, ge=0, le=10)
    after: int = Field(default=2, ge=0, le=10)
    max_chars_per_chunk: int = Field(default=8000, ge=256, le=20000)


class ResolveReferenceInput(StrictModel):
    doi: str | None = Field(default=None, max_length=256)
    citation_key: str | None = Field(default=None, max_length=256)
    item_key: str | None = Field(default=None, max_length=64)
    attachment_key: str | None = Field(default=None, max_length=64)
    title: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def exactly_one(self) -> "ResolveReferenceInput":
        if sum(value is not None for value in self.model_dump().values()) != 1:
            raise ValueError("provide exactly one reference field")
        return self


class ListFacetsInput(StrictModel):
    facet: Literal["collection", "tag", "year", "source"]
    cursor: str | None = Field(default=None, pattern=r"^[0-9]+$")
    limit: int = Field(default=50, ge=1, le=200)


class StatusInput(StrictModel):
    verbose: bool = False


class CompareVersionsInput(StrictModel):
    query: str = Field(min_length=1, max_length=4000)
    library: str = Field(min_length=1, max_length=128)
    installed_version: str = Field(min_length=1, max_length=64)
    target_version: str = Field(min_length=1, max_length=64)
    limit: int = Field(default=10, ge=1, le=50)


class WritingPatternsInput(StrictModel):
    query: str = Field(min_length=1, max_length=4000)
    section: str | None = Field(default=None, max_length=256)
    writing_function: str | None = Field(default=None, max_length=128)
    research_domain: str | None = Field(default=None, max_length=128)
    return_mode: Literal["pattern_first", "include_original"] = "pattern_first"
    limit: int = Field(default=8, ge=1, le=50)


INPUT_MODELS: dict[str, type[StrictModel]] = {
    "rag_search": SearchInput,
    "rag_get_chunk": GetChunkInput,
    "rag_get_document": GetDocumentInput,
    "rag_get_neighbors": GetNeighborsInput,
    "rag_resolve_reference": ResolveReferenceInput,
    "rag_list_facets": ListFacetsInput,
    "rag_status": StatusInput,
    "rag_compare_versions": CompareVersionsInput,
    "writing_patterns": WritingPatternsInput,
}
