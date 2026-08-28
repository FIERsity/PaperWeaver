"""Paper-native, resumable translation units and revision records."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.request import urlopen

from .models import (
    Entity,
    GlossaryEntry,
    Passage,
    PassageProvenance,
    PassageSlot,
    PdfBlock,
    TranslationContext,
    TranslationRecord,
    TranslationUnit,
)
from .pdf_markdown import materialize_markdown
from .storage import append_jsonl, atomic_write_text, read_jsonl, write_jsonl

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
    pdf_mapping = _pdf_mapping(root)
    passage_slots: list[PassageSlot] = []
    if pdf_mapping is not None:
        passages, passage_provenance, passage_slots = _pdf_passages(digest, pdf_mapping)
    else:
        passages = []
        passage_provenance = []
        section = "Preamble"
        ordinal = 0
        for line_number, block in _paragraph_blocks(source):
            if block.startswith("#"):
                section = block.lstrip("#").strip()
                continue
            # JATS visual markers remain translatable caption Passages; equations are literal.
            kind = "structural" if block.startswith("[Equation") else "paragraph"
            ordinal += 1
            passages.append(
                Passage(
                    _stable_id("psg", digest, section, ordinal, _normalise(block)),
                    section,
                    ordinal,
                    block,
                    f"source/article.md:{line_number}",
                    kind,
                )
            )
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
    if pdf_mapping is not None:
        write_jsonl(root / STATE / "passage-provenance.jsonl", passage_provenance)
        write_jsonl(root / STATE / "passage-slots.jsonl", passage_slots)
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
    pending: list[tuple[str, str]] = []
    seen: set[str] = set()
    for number, line in enumerate(draft.read_text(encoding="utf-8").splitlines(), 1):
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"{draft}:{number}: invalid JSON: {error.msg}") from error
        if set(raw) != {"passage_id", "translated_text"}:
            raise ValueError(f"{draft}:{number}: expected passage_id and translated_text")
        passage_id, text = raw["passage_id"], raw["translated_text"]
        if passage_id not in passages or not isinstance(text, str) or not text.strip():
            raise ValueError(f"{draft}:{number}: invalid passage or translation")
        if passage_id in seen:
            raise ValueError(f"{draft}:{number}: duplicate passage in one draft: {passage_id}")
        seen.add(passage_id)
        pending.append((passage_id, text))

    additions: list[TranslationRecord] = []
    source_digest = _source_digest(root)
    for passage_id, text in pending:
        previous = active.get(passage_id)
        record = TranslationRecord(
            _stable_id("tr", passage_id, previous.id if previous else "", text, reason), unit_by_passage[passage_id],
            passage_id, text, adapter, model, _now(), source_digest,
            previous.revision + 1 if previous else 1, previous.id if previous else None, reason,
        )
        additions.append(record)
        active[passage_id] = record
    if additions:
        write_jsonl(root / STATE / "translations.jsonl", records + additions)
    return len(additions)


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
    seen_ids: set[str] = set()
    active: dict[str, TranslationRecord] = {}
    for record in records:
        if record.id in seen_ids:
            raise ValueError(f"TRANSLATION_LEDGER_INVALID: duplicate record id {record.id}")
        seen_ids.add(record.id)
        previous = active.get(record.passage_id)
        expected_revision = previous.revision + 1 if previous else 1
        expected_supersedes = previous.id if previous else None
        expected_id = _stable_id(
            "tr",
            record.passage_id,
            expected_supersedes or "",
            record.translated_text,
            record.reason,
        )
        if (
            record.revision != expected_revision
            or record.supersedes != expected_supersedes
            or record.id != expected_id
        ):
            raise ValueError(
                f"TRANSLATION_LEDGER_INVALID: broken revision chain for {record.passage_id}"
            )
        active[record.passage_id] = record
    return active


def validate_translations(root: Path) -> list[str]:
    passages = read_jsonl(root / STATE / "passages.jsonl", Passage)
    active = active_translations(read_jsonl(root / STATE / "translations.jsonl", TranslationRecord))
    errors = [f"Missing translation: {item.id}" for item in passages if item.kind == "paragraph" and item.id not in active]
    errors.extend(f"Empty translation: {item.id}" for item in passages if item.id in active and not active[item.id].translated_text.strip())
    digest = _source_digest(root)
    errors.extend(
        f"Stale translation source: {item.passage_id}"
        for item in active.values()
        if item.source_sha256 != digest
    )
    if _source_format(root) == "pdf":
        slots = read_jsonl(root / STATE / "passage-slots.jsonl", PassageSlot)
        passage_ids = {item.id for item in passages if item.kind == "paragraph"}
        mapped_passages = [item.passage_id for item in slots]
        mapped_slots = [item.slot_id for item in slots]
        tree = json.loads(
            (root / "source" / "pdf" / "render-tree.json").read_text(encoding="utf-8")
        )
        expected_slots = {
            slot["slot_id"]
            for node in tree["nodes"]
            for slot in node["slots"]
            if slot["mode"] == "translate" and slot["source_text"].strip()
        }
        if len(mapped_passages) != len(set(mapped_passages)):
            errors.append("Duplicate PDF Passage slot mapping")
        if set(mapped_passages) != passage_ids:
            errors.append("PDF Passage slots do not cover translatable Passages")
        if len(mapped_slots) != len(set(mapped_slots)) or set(mapped_slots) != expected_slots:
            errors.append("PDF render-tree slots do not match Passage slots")
    return errors


def export_translated_markdown(root: Path) -> Path:
    passages = read_jsonl(root / STATE / "passages.jsonl", Passage)
    errors = validate_translations(root)
    if errors:
        raise RuntimeError("Cannot export incomplete translations: " + "; ".join(errors[:3]))
    if _source_format(root) == "pdf":
        return _export_pdf_translated_markdown(root)
    active = active_translations(read_jsonl(root / STATE / "translations.jsonl", TranslationRecord))
    paper = json.loads((root / "paper.json").read_text(encoding="utf-8"))
    lines = [f"# {paper['title']}", ""]
    lines.extend(_jats_front_matter(root))
    current_section: str | None = None
    for item in passages:
        if item.kind == "structural":
            lines.extend([item.text, ""])
        else:
            if item.section_title != current_section:
                lines.extend([f"## {_localized_section_title(item.section_title, paper['target_language'])}", ""])
                current_section = item.section_title
            translated = active[item.id].translated_text
            if _is_source_visual(item.text):
                lines.extend(_jats_visual_block(root, item.text, translated))
            else:
                lines.extend(_separate_display_formula(translated))
    lines.extend(_jats_references(root))
    output = root / "output" / "translated.md"
    output.write_text("\n".join(lines), encoding="utf-8")
    return output


def _export_pdf_translated_markdown(root: Path) -> Path:
    from .pdf_contracts import validate_pdf_project
    from .pdf_repair import applied_view

    validate_pdf_project(root, require_complete=True)
    manifest = json.loads(
        (root / "source" / "pdf" / "manifest.json").read_text(encoding="utf-8")
    )
    blocks = [PdfBlock(**item) for item in applied_view(root)]
    active = active_translations(
        read_jsonl(root / STATE / "translations.jsonl", TranslationRecord)
    )
    slots = read_jsonl(root / STATE / "passage-slots.jsonl", PassageSlot)
    slot_values = {
        item.slot_id: active[item.passage_id].translated_text for item in slots
    }
    paper = json.loads((root / "paper.json").read_text(encoding="utf-8"))
    effective = []
    for block in blocks:
        if block.kind == "document_title":
            block = replace(block, text=paper["title"])
        elif block.kind == "section_heading":
            block = replace(
                block,
                text=_localized_section_title(block.text or "", paper["target_language"]),
            )
        effective.append(block)
    asset_paths: dict[str, str] = {}
    assets = root / "source" / "assets" / "manifest.jsonl"
    for line in assets.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        source = root / item["path"]
        destination = root / "output" / "assets" / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        asset_paths[item["asset_id"]] = f"assets/{destination.name}"
    markdown, _, _ = materialize_markdown(
        effective,
        manifest["materialization_id"],
        asset_paths,
        slot_values,
    )
    visible = [
        line
        for line in markdown.splitlines()
        if not line.startswith("<!-- paperweaver:")
    ]
    output = root / "output" / "translated.md"
    atomic_write_text(output, "\n".join(visible).strip() + "\n")
    return output


def _is_source_visual(text: str) -> bool:
    return text.startswith(("[Figure:", "[Table:"))


def _separate_display_formula(text: str) -> list[str]:
    """Move a terminal numbered equation out of narrative text without changing its translation record."""
    match = re.match(r"^(?P<intro>.*?[：:])(?P<formula>[A-Za-z][^。]*?（\d+）)[。.]?$", text)
    if not match:
        return [text, ""]
    return [match.group("intro"), "", f"$$ {match.group('formula')} $$", ""]


def _jats_front_matter(root: Path) -> list[str]:
    article = _jats_article(root)
    if article is None:
        return []
    authors = []
    for item in _descendants(article, "contrib"):
        if item.attrib.get("contrib-type") != "author":
            continue
        surname = _text(_first(item, "surname"))
        given = _text(_first(item, "given-names"))
        if surname or given:
            authors.append(" ".join(part for part in (given, surname) if part))
    affiliations = [_text(item) for item in _descendants(article, "aff") if _text(item)]
    lines = []
    if authors:
        lines.extend(["## 作者", "", ", ".join(authors), ""])
    if affiliations:
        lines.extend(["## 作者单位", "", *affiliations, ""])
    return lines


def _jats_visual_block(root: Path, source_marker: str, translated_caption: str) -> list[str]:
    article = _jats_article(root)
    if article is None:
        return [translated_caption, ""]
    match = re.match(r"^\[(Figure|Table): (?:Fig|Table) (\d+)\]", source_marker)
    if match is None:
        return [translated_caption, ""]
    kind, number = match.groups()
    tag = "fig" if kind == "Figure" else "table-wrap"
    item = next((node for node in _descendants(article, tag) if _text(_first(node, "label")) in {f"Fig {number}", f"Table {number}"}), None)
    if item is None:
        return [translated_caption, ""]
    doi = _text(_first(item, "object-id"))
    if not doi:
        return [translated_caption, ""]
    prefix = "figure" if tag == "fig" else "table"
    asset = root / "output" / "assets" / f"{prefix}-{number}.png"
    _download_plos_visual(doi, asset)
    label = f"{'图' if tag == 'fig' else '表'}：{'图' if tag == 'fig' else '表'}{number}"
    caption = translated_caption.split("]", 1)[1].strip() if "]" in translated_caption else translated_caption
    return [f"![{label} {caption}](assets/{asset.name})", ""]


def _download_plos_visual(doi: str, destination: Path) -> None:
    if destination.exists() and destination.stat().st_size > 100:
        return
    url = f"https://journals.plos.org/plosone/article/figure/image?size=large&id={doi}"
    try:
        with urlopen(url, timeout=45) as response:
            data = response.read()
    except OSError as error:
        raise RuntimeError(f"Unable to download JATS visual {doi}: {error}") from error
    if not data.startswith(b"\x89PNG"):
        raise RuntimeError(f"JATS visual {doi} did not return a PNG")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)


def _jats_references(root: Path) -> list[str]:
    article = _jats_article(root)
    if article is None:
        return []
    references = [_text(item) for item in _descendants(article, "ref") if _text(item)]
    return ["## 参考文献", "", *(f"{item}" for item in references), ""] if references else []


def _jats_article(root: Path) -> ET.Element | None:
    path = root / "source" / "original.xml"
    if not path.exists():
        return None
    return ET.fromstring(path.read_bytes())


def _descendants(element: ET.Element, name: str):
    return (item for item in element.iter() if item.tag.rsplit("}", 1)[-1] == name)


def _first(element: ET.Element, name: str) -> ET.Element | None:
    return next(_descendants(element, name), None)


def _text(element: ET.Element | None) -> str:
    return " ".join("".join(element.itertext()).split()) if element is not None else ""


def _source_markdown(root: Path) -> str:
    path = root / "source" / "article.md"
    if not path.exists():
        raise FileNotFoundError("No imported paper. Run paperweaver import first.")
    return path.read_text(encoding="utf-8")


def _source_digest(root: Path) -> str:
    return json.loads((root / "source" / "source.json").read_text(encoding="utf-8"))["sha256"]


def _source_format(root: Path) -> str:
    return json.loads(
        (root / "source" / "source.json").read_text(encoding="utf-8")
    )["format"]


def _paragraph_blocks(text: str):
    start = 1
    buffered: list[str] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if line.startswith("<!-- paperweaver:"):
            if buffered:
                yield start, "\n".join(buffered).strip()
                buffered = []
            start = line_number + 1
        elif line.startswith("#"):
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


def _localized_section_title(title: str, target_language: str) -> str:
    """Localize common academic section labels while retaining section numbering."""
    if not target_language.casefold().startswith("zh"):
        return title
    replacements = {
        "Abstract": "摘要", "Introduction": "引言", "Methods": "方法", "Results": "结果", "Measurement of environmental pollution": "环境污染的测度",
        "Influencing factors of environmental pollution": "环境污染的影响因素", "DE and environmental pollution": "数字经济与环境污染",
        "Spatial econometric model": "空间计量模型", "Research hypothesis": "研究假设",
        "The direct effect of DE on environmental pollution": "数字经济对环境污染的直接效应",
        "The transmission mechanism of DE affecting environmental pollution": "数字经济影响环境污染的传导机制",
        "The threshold effect of DE on environmental pollution": "数字经济对环境污染的门槛效应",
        "The spatial spillover effect of DE on environmental pollution": "数字经济对环境污染的空间溢出效应",
        "Baseline regression model": "基准回归模型", "Variable description": "变量说明",
        "Explained variable": "被解释变量", "Explanatory variable": "解释变量", "Control variables": "控制变量",
        "Spatial weight matrix": "空间权重矩阵", "Benchmark regression results": "基准回归结果",
        "Spatial correlation test": "空间相关性检验", "Spatial econometric model regression results": "空间计量模型回归结果",
        "Municipalities are excluded": "剔除直辖市", "Core explanatory variables are replaced": "替换核心解释变量",
        "Tail reduction treatment": "缩尾处理", "Sample is replaced": "替换样本", "Exogenous shock test": "外生冲击检验",
        "Endogeneity problem": "内生性问题", "Mediation effect model": "中介效应模型",
        "Industrial structure upgrading": "产业结构升级", "Green technology innovation": "绿色技术创新",
        "Panel threshold model": "面板门槛模型", "Threshold effect test": "门槛效应检验",
        "Panel threshold model regression results": "面板门槛模型回归结果",
        "Geographical location heterogeneity": "地理位置异质性", "Resources endowment heterogeneity": "资源禀赋异质性",
        "Administrative level heterogeneity": "行政层级异质性", "Discussion": "讨论",
        "Conclusions and policy recommendations": "结论与政策建议",
    }
    for source, translated in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
        title = title.replace(source, translated)
    return title


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


def _pdf_mapping(root: Path) -> dict[str, object] | None:
    source_record = root / "source" / "source.json"
    if not source_record.exists():
        return None
    source = json.loads(source_record.read_text(encoding="utf-8"))
    if source.get("format") != "pdf":
        return None
    from .pdf_contracts import validate_pdf_project

    validate_pdf_project(root, require_complete=True)
    manifest_path = root / "source" / "pdf" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    maps = [
        json.loads(line)
        for line in (root / "source" / "article-map.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    run_id = manifest["active_run_id"]
    blocks = [
        json.loads(line)
        for line in (root / "source" / "pdf" / "runs" / run_id / "base-blocks.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    tree = json.loads(
        (root / "source" / "pdf" / "render-tree.json").read_text(encoding="utf-8")
    )
    return {
        "by_line": {item["content_start_line"]: item for item in maps},
        "blocks": {item["block_id"]: item for item in blocks},
        "tree": tree,
    }


def _pdf_passages(
    digest: str, pdf_mapping: dict[str, object]
) -> tuple[list[Passage], list[PassageProvenance], list[PassageSlot]]:
    blocks = pdf_mapping["blocks"]
    tree = pdf_mapping["tree"]
    passages: list[Passage] = []
    provenance_rows: list[PassageProvenance] = []
    slot_rows: list[PassageSlot] = []
    section = "Preamble"
    ordinal = 0
    for node in tree["nodes"]:  # type: ignore[index]
        block_ids = node["block_ids"]
        block = blocks[block_ids[0]]  # type: ignore[index]
        if block["kind"] in {"document_title", "section_heading"}:
            section = block["text"] or section
            continue
        for slot in node["slots"]:
            text = slot["source_text"]
            if slot["mode"] != "translate" or not text.strip():
                continue
            ordinal += 1
            sub_locator = slot["sub_locator"]
            first = block["provenance"][0]
            suffix = ""
            if sub_locator is not None:
                suffix = f"; table:r{sub_locator['row'] + 1}c{sub_locator['column'] + 1}"
            locator = f"pdf:p{first['page']}{first['bbox']}{suffix}"
            passage = Passage(
                _stable_id("psg", digest, section, ordinal, _normalise(text)),
                section,
                ordinal,
                text,
                locator,
                "paragraph",
            )
            passages.append(passage)
            provenance_rows.append(
                PassageProvenance(
                    1, passage.id, block_ids, sub_locator, block["provenance"]
                )
            )
            slot_rows.append(
                PassageSlot(
                    1,
                    slot["slot_id"],
                    node["node_id"],
                    passage.id,
                    slot["role"],
                    block_ids,
                    sub_locator,
                )
            )
    return passages, provenance_rows, slot_rows


def _source_locator(line_number: int, pdf_mapping: dict[str, object] | None) -> str:
    locator = f"source/article.md:{line_number}"
    if pdf_mapping is None:
        return locator
    mapping = pdf_mapping["by_line"].get(line_number)  # type: ignore[index,union-attr]
    if mapping is None:
        return locator
    blocks = pdf_mapping["blocks"]  # type: ignore[assignment]
    first = blocks[mapping["block_ids"][0]]["provenance"][0]  # type: ignore[index]
    return f"{locator}; pdf:p{first['page']}{first['bbox']}"
