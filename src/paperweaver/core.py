"""File-first project and conservative Markdown paper structure operations."""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .models import Paper, PaperInventory, PaperSection, PaperSource


def init_project(root: Path, title: str, source_language: str, target_language: str) -> Paper:
    if (root / "paper.json").exists():
        raise FileExistsError(f"Project already exists: {root}")
    (root / "source").mkdir(parents=True)
    (root / "output").mkdir()
    paper = Paper(title, source_language, target_language)
    _write_json(root / "paper.json", paper.to_dict())
    return paper


def import_paper(root: Path, source: Path) -> PaperSource:
    if source.suffix.lower() not in {".md", ".markdown", ".txt", ".xml"}:
        raise ValueError("Version 0.4 supports Markdown, TXT, and JATS XML; DOCX/PDF are not supported")
    _require_project(root)
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    destination = root / "source" / "article.md"
    if destination.exists() and destination.read_bytes() != data:
        raise FileExistsError("A different source is already imported; create a new project")
    source_format = "jats" if source.suffix.lower() == ".xml" else source.suffix.lower().lstrip(".")
    if source_format == "jats":
        markdown, title, inventory = _import_jats(data)
        destination.write_text(markdown, encoding="utf-8")
        original = root / "source" / "original.xml"
        original.write_bytes(data)
    else:
        text = data.decode("utf-8")
        destination.write_text(_normalise_text_source(text, source_format), encoding="utf-8")
        title = _first_heading(text) or _title_from_text(text) or _require_project(root).title
        original = None
        inventory = PaperInventory(source_format)
    record = PaperSource(
        title, str(destination.relative_to(root)), digest, source_format,
        str(original.relative_to(root)) if original else None,
    )
    _write_json(root / "source" / "source.json", record.to_dict())
    _write_json(root / "source" / "inventory.json", inventory.to_dict())
    return record


def parse_sections(text: str) -> list[PaperSection]:
    lines = text.splitlines()
    headings = [(index, match) for index, line in enumerate(lines) if (match := re.match(r"^(#{1,6})\s+(.+?)\s*$", line))]
    sections: list[PaperSection] = []
    for ordinal, (start, match) in enumerate(headings):
        end = headings[ordinal + 1][0] if ordinal + 1 < len(headings) else len(lines)
        title = match.group(2)
        body = " ".join(lines[start + 1 : end]).strip()
        sections.append(PaperSection(title, len(match.group(1)), start + 1, end, len(body.split()), title.casefold() == "abstract"))
    return sections


def _import_jats(data: bytes) -> tuple[str, str, PaperInventory]:
    try:
        article = ET.fromstring(data)
    except ET.ParseError as error:
        raise ValueError(f"Invalid JATS XML: {error}") from error
    if _local(article.tag) != "article":
        raise ValueError("XML import supports JATS article documents only")
    title = _text(_first(article, "article-title")) or "Untitled paper"
    abstract = _text(_first(article, "abstract"))
    lines = [f"# {title}"]
    if abstract:
        lines.extend(["", "## Abstract", "", abstract])
    body = _first(article, "body")
    if body is None:
        body = article
    for section in _children(body, "sec"):
        _append_jats_section(lines, section, 2)
    figures = list(_descendants(article, "fig"))
    tables = list(_descendants(article, "table-wrap"))
    equations = list(_descendants(article, "disp-formula"))
    citations = [item for item in _descendants(article, "xref") if item.attrib.get("ref-type") == "bibr"]
    references = list(_descendants(article, "ref"))
    warnings = []
    if figures:
        warnings.append("Figure binaries are not fetched in version 0.2; captions are retained as markers.")
    if equations:
        warnings.append("Equations are retained as protected text markers; mathematical layout is not rendered.")
    return "\n".join(lines).strip() + "\n", title, PaperInventory(
        "jats", len(figures), len(tables), len(equations), len(citations), len(references), warnings
    )


def _append_jats_section(lines: list[str], section: ET.Element, level: int) -> None:
    title = _text(_first(section, "title")) or "Untitled section"
    lines.extend(["", f"{'#' * min(level, 6)} {title}"])
    for child in section:
        kind = _local(child.tag)
        if kind == "p":
            value = _text(child)
            if value:
                lines.extend(["", value])
        elif kind == "fig":
            lines.extend(["", f"[Figure: {_text(_first(child, 'label')) or 'unlabeled'}] {_text(_first(child, 'caption'))}"])
        elif kind == "table-wrap":
            lines.extend(["", f"[Table: {_text(_first(child, 'label')) or 'unlabeled'}] {_text(_first(child, 'caption'))}"])
        elif kind == "disp-formula":
            lines.extend(["", f"[Equation] {_text(child)}"])
        elif kind == "sec":
            _append_jats_section(lines, child, level + 1)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [item for item in element if _local(item.tag) == name]


def _descendants(element: ET.Element, name: str):
    return (item for item in element.iter() if _local(item.tag) == name)


def _first(element: ET.Element, name: str) -> ET.Element | None:
    return next(_descendants(element, name), None)


def _text(element: ET.Element | None) -> str:
    return " ".join("".join(element.itertext()).split()) if element is not None else ""


def _normalise_text_source(text: str, source_format: str) -> str:
    if source_format not in {"txt", "text"}:
        return text
    lines = []
    for line in text.splitlines():
        if line.strip().startswith("TITLE:") and line.strip().removeprefix("TITLE:").strip():
            lines.append(f"# {line.strip().removeprefix('TITLE:').strip()}")
            continue
        match = re.match(r"^(\d+(?:\.\d+)*)\s+([A-Za-z][A-Za-z ,&'’()\-]{2,80})$", line.strip())
        if match and not match.group(2).endswith((".", ",", ";", ":")):
            lines.append(f"## {match.group(1)} {match.group(2)}")
        else:
            lines.append(line)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def _title_from_text(text: str) -> str | None:
    for line in text.splitlines():
        value = line.strip()
        if value.startswith("TITLE:") and value.removeprefix("TITLE:").strip():
            return value.removeprefix("TITLE:").strip()
        if value and not value.startswith(("TITLE:", "YEAR:", "DOI:", "URL:")) and len(value) < 180:
            return value
    return None


def _first_heading(text: str) -> str | None:
    match = re.search(r"^#\s+(.+?)\s*$", text, re.MULTILINE)
    return match.group(1) if match else None


def _require_project(root: Path) -> Paper:
    path = root / "paper.json"
    if not path.exists():
        raise FileNotFoundError(f"Not a PaperWeaver project: {root}")
    return Paper(**json.loads(path.read_text(encoding="utf-8")))


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
