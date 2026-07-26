"""Transparent records for one academic-paper workspace."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


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


@dataclass(frozen=True)
class PaperSection(Record):
    title: str
    level: int
    start_line: int
    end_line: int
    word_count: int
    is_abstract: bool = False


@dataclass(frozen=True)
class ReadingGuide(Record):
    title: str
    abstract: str | None
    sections: list[PaperSection] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
