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
