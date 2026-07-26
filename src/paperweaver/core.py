"""File-first project and conservative Markdown paper structure operations."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

from .models import Paper, PaperSection, PaperSource, ReadingGuide


def init_project(root: Path, title: str, source_language: str, target_language: str) -> Paper:
    if (root / "paper.json").exists():
        raise FileExistsError(f"Project already exists: {root}")
    (root / "source").mkdir(parents=True)
    (root / "output").mkdir()
    paper = Paper(title, source_language, target_language)
    _write_json(root / "paper.json", paper.to_dict())
    return paper


def import_paper(root: Path, source: Path) -> PaperSource:
    if source.suffix.lower() not in {".md", ".markdown", ".txt"}:
        raise ValueError("Version 0.1 supports Markdown and TXT; JATS/DOCX/PDF are planned")
    _require_project(root)
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    destination = root / "source" / "article.md"
    if destination.exists() and destination.read_bytes() != data:
        raise FileExistsError("A different source is already imported; create a new project")
    shutil.copyfile(source, destination)
    title = _first_heading(data.decode("utf-8")) or _require_project(root).title
    record = PaperSource(title, str(destination.relative_to(root)), digest, source.suffix.lower().lstrip("."))
    _write_json(root / "source" / "source.json", record.to_dict())
    return record


def build_reading_guide(root: Path) -> ReadingGuide:
    source = root / "source" / "article.md"
    if not source.exists():
        raise FileNotFoundError("No imported paper. Run paperweaver import first.")
    text = source.read_text(encoding="utf-8")
    sections = parse_sections(text)
    title = _first_heading(text) or _require_project(root).title
    abstract = next((section_text(text, section) for section in sections if section.is_abstract), None)
    guide = ReadingGuide(title, abstract, sections, _questions(sections))
    _write_json(root / "output" / "reading-guide.json", guide.to_dict())
    _write_markdown_guide(root / "output" / "reading-guide.md", guide)
    return guide


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


def section_text(text: str, section: PaperSection) -> str:
    return "\n".join(text.splitlines()[section.start_line : section.end_line]).strip()


def _questions(sections: list[PaperSection]) -> list[str]:
    names = {section.title.casefold() for section in sections}
    prompts = ["What question does the paper set out to answer?", "Which claims are directly supported by the reported evidence?"]
    if any(name in names for name in ("methods", "methodology", "materials and methods")):
        prompts.append("What does the method identify, and what does it leave unidentified?")
    if any(name in names for name in ("results", "findings")):
        prompts.append("Which result is central, and how large or uncertain is it?")
    if any(name in names for name in ("discussion", "conclusion", "limitations")):
        prompts.append("Where do the authors distinguish evidence from interpretation or limitation?")
    return prompts


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


def _write_markdown_guide(path: Path, guide: ReadingGuide) -> None:
    lines = [f"# Reading guide: {guide.title}", "", "## Structure", ""]
    lines.extend(f"- {section.title} ({section.word_count} words)" for section in guide.sections)
    if guide.abstract:
        lines.extend(["", "## Abstract", "", guide.abstract])
    lines.extend(["", "## Questions", ""])
    lines.extend(f"- {question}" for question in guide.questions)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
