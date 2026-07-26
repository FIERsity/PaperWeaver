"""Source-grounded paper orientation without invented research claims."""

from __future__ import annotations

from pathlib import Path

from .models import GuidePoint, Passage
from .storage import read_jsonl, write_jsonl


def build_argument_map(root: Path) -> list[GuidePoint]:
    """Map paper sections to reading tasks and cite the exact supporting Passage."""
    passages = read_jsonl(root / "state" / "passages.jsonl", Passage)
    categories = {
        "question": ("introduction", "background", "abstract"),
        "method": ("method", "materials", "data", "design", "identification"),
        "evidence": ("result", "finding", "analysis"),
        "boundary": ("discussion", "conclusion", "limitation"),
    }
    points: list[GuidePoint] = []
    for category, hints in categories.items():
        matching = [
            item for item in passages
            if item.kind == "paragraph" and any(hint in item.section_title.casefold() for hint in hints)
        ]
        if matching:
            item = matching[0]
            points.append(GuidePoint(
                category, _statement(category, item.section_title), [item.id], 0.7
            ))
    write_jsonl(root / "output" / "argument-map.jsonl", points)
    _write_markdown(root / "output" / "argument-map.md", points)
    return points


def _statement(category: str, title: str) -> str:
    templates = {
        "question": f"Read '{title}' to identify the research question and stated motivation.",
        "method": f"Read '{title}' to determine the data, design, assumptions, and identification limits.",
        "evidence": f"Read '{title}' to separate reported results from their interpretation.",
        "boundary": f"Read '{title}' to locate stated limitations, scope conditions, and conclusion boundaries.",
    }
    return templates[category]


def _write_markdown(path: Path, points: list[GuidePoint]) -> None:
    labels = {"question": "Research question", "method": "Method and identification", "evidence": "Evidence", "boundary": "Conclusion boundary"}
    lines = ["# Argument map", ""]
    for point in points:
        lines.extend([f"## {labels[point.category]}", "", point.statement, "", f"Evidence: {', '.join(point.evidence_passage_ids)}", ""])
    path.write_text("\n".join(lines), encoding="utf-8")
