"""Transparent records for one academic-paper workspace."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


class Record:
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Paper(Record):
    title: str
    source_language: str
    target_language: str
    schema_version: int = 1


@dataclass(frozen=True)
class PaperSource(Record):
    title: str
    path: str
    sha256: str
    format: str
    original_path: str | None = None


@dataclass(frozen=True)
class PaperInventory(Record):
    source_format: str
    figures: int = 0
    tables: int = 0
    equations: int = 0
    citations: int = 0
    references: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PaperSection(Record):
    title: str
    level: int
    start_line: int
    end_line: int
    word_count: int
    is_abstract: bool = False


@dataclass(frozen=True)
class Passage(Record):
    """The smallest translatable paper unit; ID remains stable for unchanged source text."""

    id: str
    section_title: str
    ordinal: int
    text: str
    source_locator: str
    kind: Literal["paragraph", "structural"] = "paragraph"


@dataclass(frozen=True)
class TranslationUnit(Record):
    id: str
    section_title: str
    passage_ids: list[str]
    ordinal: int


@dataclass(frozen=True)
class GlossaryEntry(Record):
    term: str
    preferred_translation: str
    evidence_passage_ids: list[str]
    confidence: float
    status: Literal["proposed", "approved", "rejected"] = "proposed"
    note: str = ""


@dataclass(frozen=True)
class Entity(Record):
    name: str
    kind: str
    evidence_passage_ids: list[str]
    confidence: float
    status: Literal["proposed", "approved", "rejected"] = "proposed"


@dataclass(frozen=True)
class TranslationContext(Record):
    unit_id: str
    passages: list[Passage]
    previous_text: str | None
    next_text: str | None
    glossary: list[GlossaryEntry]
    entities: list[Entity]
    source_language: str
    target_language: str


@dataclass(frozen=True)
class TranslationRecord(Record):
    id: str
    unit_id: str
    passage_id: str
    translated_text: str
    adapter: str
    model: str
    created_at: str
    source_sha256: str
    revision: int = 1
    supersedes: str | None = None
    reason: str = "initial"


@dataclass(frozen=True)
class ChineseSummaryRecord(Record):
    """A reviewable Chinese whole-paper summary, separate from source and translation records."""

    id: str
    overview: str
    methods: str
    conclusions: str
    limitations: str
    evidence_passage_ids: list[str]
    adapter: str
    model: str
    created_at: str
    supersedes: str | None = None


@dataclass(frozen=True)
class PdfProvenance(Record):
    page: int
    bbox: list[float]
    page_width: float
    page_height: float
    media_box: list[float]
    crop_box: list[float]
    rotation: int
    coord_space: Literal["pdf_points"] = "pdf_points"
    origin: Literal["top_left"] = "top_left"


@dataclass(frozen=True)
class PdfBlock(Record):
    """A canonical, source-located block derived from an immutable PDF run."""

    schema_version: int
    block_id: str
    source_sha256: str
    run_id: str
    ordinal: int
    kind: str
    status: Literal["ok", "flagged", "unresolved"]
    disposition: Literal["render", "excluded_artifact", "unresolved_placeholder"]
    confidence: dict[str, float | None]
    provenance: list[dict[str, Any]]
    source_object_refs: list[str]
    raw_text: str | None
    text: str | None
    metadata_role: str | None
    list: dict[str, Any] | None
    asset_refs: list[str]
    table: dict[str, Any] | None
    equation: dict[str, Any] | None
    transformations: list[dict[str, Any]]
    issues: list[str]
    backend_ref: str


@dataclass(frozen=True)
class PdfObjectAccounting(Record):
    schema_version: int
    object_ref: str
    object_kind: str
    primary_disposition: Literal["rendered", "excluded_artifact", "unresolved", "duplicate"]
    primary_block_id: str | None
    supporting_block_ids: list[str]
    duplicate_of: str | None
    reason_code: str | None


@dataclass(frozen=True)
class PdfRelation(Record):
    schema_version: int
    relation_id: str
    type: str
    from_block_ids: list[str]
    to_block_ids: list[str]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class PdfArticleMap(Record):
    schema_version: int
    render_node_id: str
    anchor_block_id: str | None
    markdown_anchor_line: int
    content_start_line: int
    content_end_line: int
    block_ids: list[str]


@dataclass(frozen=True)
class PassageProvenance(Record):
    schema_version: int
    passage_id: str
    block_ids: list[str]
    sub_locator: dict[str, Any] | None
    provenance: list[dict[str, Any]]


@dataclass(frozen=True)
class PassageSlot(Record):
    """Bind one translatable render-tree slot to one Passage."""

    schema_version: int
    slot_id: str
    node_id: str
    passage_id: str
    role: str
    block_ids: list[str]
    sub_locator: dict[str, Any] | None
