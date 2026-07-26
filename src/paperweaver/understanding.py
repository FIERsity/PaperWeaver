"""Source-grounded paper orientation without invented research claims."""

from __future__ import annotations

import json
from pathlib import Path

from .models import GuidePoint, Passage
from .storage import read_jsonl, write_jsonl


def build_argument_map(root: Path) -> list[GuidePoint]:
    """Map paper sections to reading tasks and cite the exact supporting Passage."""
    passages = read_jsonl(root / "state" / "passages.jsonl", Passage)
    target_language = json.loads((root / "paper.json").read_text(encoding="utf-8"))["target_language"]
    categories = {
        "question": ("introduction", "background", "abstract"),
        "method": ("4.", "method", "materials", "data", "design", "identification"),
        "evidence": ("5.", "result", "finding", "analysis"),
        "boundary": ("7.", "discussion", "conclusion", "limitation"),
    }
    points: list[GuidePoint] = []
    for category, hints in categories.items():
        matching = _matching_passages(passages, hints)
        if matching:
            item = matching[0]
            points.append(GuidePoint(
                category, _statement(category, item.section_title, target_language), [item.id], 0.7
            ))
    write_jsonl(root / "output" / "argument-map.jsonl", points)
    _write_markdown(root / "output" / "argument-map.md", points, target_language)
    return points


def _matching_passages(passages: list[Passage], hints: tuple[str, ...]) -> list[Passage]:
    """Respect ordered section-label preference: Introduction before Abstract, section 4 before loose words."""
    for hint in hints:
        matches = [
            item for item in passages
            if item.kind == "paragraph" and hint in item.section_title.casefold()
        ]
        if matches:
            return matches
    return []


def _statement(category: str, title: str, target_language: str) -> str:
    if target_language.casefold().startswith("zh"):
        templates = {
            "question": f"请阅读“{title}”，识别论文明确提出的研究问题与研究动机。",
            "method": f"请阅读“{title}”，辨析数据、研究设计、识别假设及其限制。",
            "evidence": f"请阅读“{title}”，区分报告的结果与作者对结果的解释。",
            "boundary": f"请阅读“{title}”，定位作者明确陈述的局限、适用范围与结论边界。",
        }
        return templates[category]
    templates = {
        "question": f"Read '{title}' to identify the research question and stated motivation.",
        "method": f"Read '{title}' to determine the data, design, assumptions, and identification limits.",
        "evidence": f"Read '{title}' to separate reported results from their interpretation.",
        "boundary": f"Read '{title}' to locate stated limitations, scope conditions, and conclusion boundaries.",
    }
    return templates[category]


def _write_markdown(path: Path, points: list[GuidePoint], target_language: str) -> None:
    chinese = target_language.casefold().startswith("zh")
    labels = {"question": "Research question", "method": "Method and identification", "evidence": "Evidence", "boundary": "Conclusion boundary"}
    lines = ["# 论证阅读地图" if chinese else "# Argument map", ""]
    for point in points:
        lines.extend([f"## {labels[point.category] if not chinese else {'question': '研究问题', 'method': '方法与识别', 'evidence': '证据', 'boundary': '结论边界'}[point.category]}", "", point.statement, "", f"{'证据' if chinese else 'Evidence'}: {', '.join(point.evidence_passage_ids)}", ""])
    path.write_text("\n".join(lines), encoding="utf-8")
