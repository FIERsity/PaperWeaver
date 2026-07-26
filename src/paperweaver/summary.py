"""Reviewable Chinese full-paper summaries with explicit source evidence."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from .models import ChineseSummaryRecord, Passage
from .storage import append_jsonl, read_jsonl


def import_chinese_summary(root: Path, draft: Path, adapter: str, model: str) -> ChineseSummaryRecord:
    raw = json.loads(draft.read_text(encoding="utf-8"))
    expected = {"overview", "methods", "conclusions", "limitations", "evidence_passage_ids"}
    if set(raw) != expected:
        raise ValueError(f"{draft}: expected exactly {sorted(expected)}")
    if any(not isinstance(raw[name], str) or not raw[name].strip() for name in expected - {"evidence_passage_ids"}):
        raise ValueError(f"{draft}: every summary field must be a non-empty string")
    evidence = raw["evidence_passage_ids"]
    passage_ids = {item.id for item in read_jsonl(root / "state" / "passages.jsonl", Passage)}
    if not isinstance(evidence, list) or not evidence or set(evidence) - passage_ids:
        raise ValueError(f"{draft}: evidence_passage_ids must reference existing Passages")
    history = read_jsonl(root / "state" / "chinese-summaries.jsonl", ChineseSummaryRecord)
    previous = history[-1] if history else None
    record = ChineseSummaryRecord(
        _summary_id(raw, previous.id if previous else ""), raw["overview"], raw["methods"],
        raw["conclusions"], raw["limitations"], evidence, adapter, model, _now(),
        previous.id if previous else None,
    )
    append_jsonl(root / "state" / "chinese-summaries.jsonl", record)
    return record


def export_chinese_summary(root: Path) -> Path:
    records = read_jsonl(root / "state" / "chinese-summaries.jsonl", ChineseSummaryRecord)
    if not records:
        raise RuntimeError("No Chinese summary. Run paperweaver summary-import first.")
    record = records[-1]
    paper = json.loads((root / "paper.json").read_text(encoding="utf-8"))
    lines = [
        f"# {paper['title']}：中文全文摘要", "", "## 全文摘要", "", record.overview, "",
        "## 方法", "", record.methods, "", "## 结论", "", record.conclusions, "",
        "## 局限性", "", record.limitations, "", "## 原文依据", "",
        ", ".join(record.evidence_passage_ids), "",
    ]
    output = root / "output" / "中文全文摘要.md"
    output.write_text("\n".join(lines), encoding="utf-8")
    return output


def _summary_id(raw: dict[str, object], predecessor: str) -> str:
    content = json.dumps(raw, ensure_ascii=False, sort_keys=True)
    return "summary_" + hashlib.sha256(f"{predecessor}\x1f{content}".encode()).hexdigest()[:16]


def _now() -> str:
    return datetime.now(UTC).isoformat()
