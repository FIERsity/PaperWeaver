"""Paper-native, resumable translation units and revision records."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .models import (
    Entity,
    GlossaryEntry,
    Passage,
    TranslationContext,
    TranslationRecord,
    TranslationUnit,
)
from .storage import append_jsonl, read_jsonl, write_jsonl

STATE = "state"


class TranslationAdapter(Protocol):
    name: str
    model: str

    def translate(self, context: TranslationContext) -> list[str]: ...


class MockTranslationAdapter:
    name = "mock"
    model = "deterministic-copy"

    def translate(self, context: TranslationContext) -> list[str]:
        return [f"[MOCK zh-CN] {passage.text}" for passage in context.passages]


def segment_paper(root: Path, unit_size: int = 2) -> tuple[list[Passage], list[TranslationUnit]]:
    if unit_size < 1:
        raise ValueError("unit_size must be at least 1")
    source = _source_markdown(root)
    digest = _source_digest(root)
    passages: list[Passage] = []
    section = "Preamble"
    ordinal = 0
    for line_number, block in _paragraph_blocks(source):
        if block.startswith("#"):
            section = block.lstrip("#").strip()
            continue
        if block.startswith("[") and block.split("]", 1)[0] in {"[Figure", "[Table", "[Equation"}:
            kind = "structural"
        else:
            kind = "paragraph"
        ordinal += 1
        passages.append(Passage(
            _stable_id("psg", digest, section, ordinal, _normalise(block)), section, ordinal, block,
            f"source/article.md:{line_number}", kind,
        ))
    units: list[TranslationUnit] = []
    for section_title in dict.fromkeys(item.section_title for item in passages):
        scoped = [item for item in passages if item.section_title == section_title]
        for offset in range(0, len(scoped), unit_size):
            group = scoped[offset : offset + unit_size]
            units.append(TranslationUnit(
                _stable_id("unit", digest, section_title, *(item.id for item in group)), section_title,
                [item.id for item in group], len(units) + 1,
            ))
    write_jsonl(root / STATE / "passages.jsonl", passages)
    write_jsonl(root / STATE / "units.jsonl", units)
    for name in ("glossary.jsonl", "entities.jsonl", "translations.jsonl"):
        path = root / STATE / name
        if not path.exists():
            write_jsonl(path, [])
    return passages, units


def build_context(root: Path, unit: TranslationUnit) -> TranslationContext:
    passages = read_jsonl(root / STATE / "passages.jsonl", Passage)
    by_id = {item.id: item for item in passages}
    selected = [by_id[item_id] for item_id in unit.passage_ids]
    first = passages.index(selected[0])
    last = passages.index(selected[-1])
    glossary = [item for item in read_jsonl(root / STATE / "glossary.jsonl", GlossaryEntry) if item.status == "approved"]
    entities = [item for item in read_jsonl(root / STATE / "entities.jsonl", Entity) if item.status == "approved"]
    paper = json.loads((root / "paper.json").read_text(encoding="utf-8"))
    return TranslationContext(
        unit.id, selected, passages[first - 1].text if first else None,
        passages[last + 1].text if last + 1 < len(passages) else None,
        glossary, entities, paper["source_language"], paper["target_language"],
    )


def translate_paper(
    root: Path, adapter: TranslationAdapter, *, passage_ids: set[str] | None = None,
    reason: str = "initial", max_units: int | None = None,
) -> tuple[int, int]:
    units = read_jsonl(root / STATE / "units.jsonl", TranslationUnit)
    if not units:
        raise RuntimeError("No translation units. Run paperweaver segment first.")
    records = read_jsonl(root / STATE / "translations.jsonl", TranslationRecord)
    active = active_translations(records)
    written = skipped = processed = 0
    for unit in units:
        context = build_context(root, unit)
        selected = [
            item for item in context.passages
            if item.kind == "paragraph" and (passage_ids is None or item.id in passage_ids)
        ]
        pending = [item for item in selected if passage_ids is not None or item.id not in active]
        if not pending:
            skipped += len(selected)
            continue
        if max_units is not None and processed >= max_units:
            break
        output_context = TranslationContext(
            context.unit_id, pending, context.previous_text, context.next_text, context.glossary,
            context.entities, context.source_language, context.target_language,
        )
        outputs = adapter.translate(output_context)
        if len(outputs) != len(pending) or any(not item.strip() for item in outputs):
            raise RuntimeError("Adapter must return one non-empty translation per requested Passage")
        for passage, output in zip(pending, outputs, strict=True):
            previous = active.get(passage.id)
            record = TranslationRecord(
                _stable_id("tr", passage.id, previous.id if previous else "", output, reason), unit.id,
                passage.id, output, adapter.name, adapter.model, _now(), _source_digest(root),
                previous.revision + 1 if previous else 1, previous.id if previous else None, reason,
            )
            append_jsonl(root / STATE / "translations.jsonl", record)
            active[passage.id] = record
            written += 1
        processed += 1
    return written, skipped


def import_translation_draft(root: Path, draft: Path, adapter: str, model: str, reason: str) -> int:
    passages = {item.id for item in read_jsonl(root / STATE / "passages.jsonl", Passage)}
    records = read_jsonl(root / STATE / "translations.jsonl", TranslationRecord)
    active = active_translations(records)
    unit_by_passage = {item_id: unit.id for unit in read_jsonl(root / STATE / "units.jsonl", TranslationUnit) for item_id in unit.passage_ids}
    written = 0
    for number, line in enumerate(draft.read_text(encoding="utf-8").splitlines(), 1):
        raw = json.loads(line)
        if set(raw) != {"passage_id", "translated_text"}:
            raise ValueError(f"{draft}:{number}: expected passage_id and translated_text")
        passage_id, text = raw["passage_id"], raw["translated_text"]
        if passage_id not in passages or not isinstance(text, str) or not text.strip():
            raise ValueError(f"{draft}:{number}: invalid passage or translation")
        previous = active.get(passage_id)
        record = TranslationRecord(
            _stable_id("tr", passage_id, previous.id if previous else "", text, reason), unit_by_passage[passage_id],
            passage_id, text, adapter, model, _now(), _source_digest(root),
            previous.revision + 1 if previous else 1, previous.id if previous else None, reason,
        )
        append_jsonl(root / STATE / "translations.jsonl", record)
        active[passage_id] = record
        written += 1
    return written


def import_glossary(root: Path, draft: Path) -> int:
    """Append evidence-backed terminology without overwriting an existing row."""
    passages = {item.id for item in read_jsonl(root / STATE / "passages.jsonl", Passage)}
    existing = {item.term.casefold() for item in read_jsonl(root / STATE / "glossary.jsonl", GlossaryEntry)}
    written = 0
    for number, line in enumerate(draft.read_text(encoding="utf-8").splitlines(), 1):
        raw = json.loads(line)
        expected = {"term", "preferred_translation", "evidence_passage_ids", "confidence", "status", "note"}
        if set(raw) != expected:
            raise ValueError(f"{draft}:{number}: expected exactly {sorted(expected)}")
        entry = GlossaryEntry(**raw)
        _validate_evidence(entry.evidence_passage_ids, passages, entry.confidence, draft, number)
        if entry.term.casefold() in existing:
            raise ValueError(f"{draft}:{number}: glossary term already exists: {entry.term}")
        append_jsonl(root / STATE / "glossary.jsonl", entry)
        existing.add(entry.term.casefold())
        written += 1
    return written


def import_entities(root: Path, draft: Path) -> int:
    """Append evidence-backed entities without overwriting an existing row."""
    passages = {item.id for item in read_jsonl(root / STATE / "passages.jsonl", Passage)}
    existing = {item.name.casefold() for item in read_jsonl(root / STATE / "entities.jsonl", Entity)}
    written = 0
    for number, line in enumerate(draft.read_text(encoding="utf-8").splitlines(), 1):
        raw = json.loads(line)
        expected = {"name", "kind", "evidence_passage_ids", "confidence", "status"}
        if set(raw) != expected:
            raise ValueError(f"{draft}:{number}: expected exactly {sorted(expected)}")
        entry = Entity(**raw)
        _validate_evidence(entry.evidence_passage_ids, passages, entry.confidence, draft, number)
        if entry.name.casefold() in existing:
            raise ValueError(f"{draft}:{number}: entity already exists: {entry.name}")
        append_jsonl(root / STATE / "entities.jsonl", entry)
        existing.add(entry.name.casefold())
        written += 1
    return written


def active_translations(records: list[TranslationRecord]) -> dict[str, TranslationRecord]:
    return {record.passage_id: record for record in records}


def validate_translations(root: Path) -> list[str]:
    passages = read_jsonl(root / STATE / "passages.jsonl", Passage)
    active = active_translations(read_jsonl(root / STATE / "translations.jsonl", TranslationRecord))
    errors = [f"Missing translation: {item.id}" for item in passages if item.kind == "paragraph" and item.id not in active]
    errors.extend(f"Empty translation: {item.id}" for item in passages if item.id in active and not active[item.id].translated_text.strip())
    return errors


def export_bilingual_markdown(root: Path) -> Path:
    passages = read_jsonl(root / STATE / "passages.jsonl", Passage)
    errors = validate_translations(root)
    if errors:
        raise RuntimeError("Cannot export incomplete translations: " + "; ".join(errors[:3]))
    active = active_translations(read_jsonl(root / STATE / "translations.jsonl", TranslationRecord))
    lines = ["# Bilingual paper draft", ""]
    for item in passages:
        if item.kind == "structural":
            lines.extend([item.text, ""])
        else:
            lines.extend([f"> {item.text}", "", active[item.id].translated_text, ""])
    output = root / "output" / "bilingual.md"
    output.write_text("\n".join(lines), encoding="utf-8")
    return output


def _source_markdown(root: Path) -> str:
    path = root / "source" / "article.md"
    if not path.exists():
        raise FileNotFoundError("No imported paper. Run paperweaver import first.")
    return path.read_text(encoding="utf-8")


def _source_digest(root: Path) -> str:
    return json.loads((root / "source" / "source.json").read_text(encoding="utf-8"))["sha256"]


def _paragraph_blocks(text: str):
    start = 1
    buffered: list[str] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if line.startswith("#"):
            if buffered:
                yield start, "\n".join(buffered).strip()
                buffered = []
            yield line_number, line.strip()
            start = line_number + 1
        elif line.strip():
            if not buffered:
                start = line_number
            buffered.append(line)
        elif buffered:
            yield start, "\n".join(buffered).strip()
            buffered = []
            start = line_number + 1
    if buffered:
        yield start, "\n".join(buffered).strip()


def _normalise(text: str) -> str:
    return " ".join(text.split())


def _validate_evidence(
    evidence: list[str], passages: set[str], confidence: float, draft: Path, number: int
) -> None:
    if not evidence or set(evidence) - passages:
        raise ValueError(f"{draft}:{number}: evidence_passage_ids must reference existing Passages")
    if not 0 <= confidence <= 1:
        raise ValueError(f"{draft}:{number}: confidence must be between 0 and 1")


def _stable_id(prefix: str, *parts: object) -> str:
    value = "\x1f".join(map(str, parts))
    return f"{prefix}_{hashlib.sha256(value.encode()).hexdigest()[:16]}"


def _now() -> str:
    return datetime.now(UTC).isoformat()
