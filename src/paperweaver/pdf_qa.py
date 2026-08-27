"""Deterministic PDF import metrics, issues, and completion gate."""

from __future__ import annotations

from collections import Counter
from typing import Any

from .models import PdfBlock
from .pdf_contracts import stable_id


def build_qa(
    source_sha256: str,
    run_id: str,
    materialization_id: str,
    policy: dict[str, Any],
    policy_sha256: str,
    metrics: dict[str, Any],
    blocks: list[PdfBlock],
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    unresolved = [block for block in blocks if block.status == "unresolved"]
    flagged = [block for block in blocks if block.status == "flagged"]
    for block in unresolved:
        for code in block.issues or ["PDF_BLOCK_UNRESOLVED"]:
            issues.append(_issue(code, "incomplete", block))
    for block in flagged:
        for code in block.issues or ["PDF_BLOCK_FLAGGED"]:
            issues.append(_issue(code, "warning", block))
    if not any(block.kind == "document_title" for block in blocks):
        issues.append(
            {
                "issue_id": stable_id("issue", "DOCUMENT_TITLE_UNRESOLVED", source_sha256),
                "severity": "incomplete",
                "code": "DOCUMENT_TITLE_UNRESOLVED",
                "message": "No source-grounded document title could be identified.",
                "block_ids": [],
                "provenance": [],
                "asset_refs": [],
            }
        )

    if metrics["content_pages_with_usable_text_ratio"] < policy["min_usable_text_page_ratio"]:
        issues.append(
            _metric_issue(
                "PDF_TEXT_LAYER_UNSUPPORTED",
                "unsupported",
                metrics["content_pages_with_usable_text_ratio"],
                policy["min_usable_text_page_ratio"],
            )
        )
    if metrics["valid_unicode_ratio"] < policy["min_valid_unicode_ratio"]:
        issues.append(
            _metric_issue(
                "PDF_TEXT_ENCODING_UNUSABLE",
                "incomplete",
                metrics["valid_unicode_ratio"],
                policy["min_valid_unicode_ratio"],
            )
        )
    if metrics.get("unresolved_glyphs", 0):
        count = int(metrics["unresolved_glyphs"])
        issues.append(
            {
                "issue_id": stable_id("issue", "PDF_UNRESOLVED_GLYPHS", count),
                "severity": "incomplete",
                "code": "PDF_UNRESOLVED_GLYPHS",
                "message": f"{count} glyphs have no trustworthy Unicode mapping.",
                "block_ids": [],
                "provenance": [],
                "asset_refs": [],
            }
        )
    replacement_ratio = metrics["replacement_character_ratio"]
    if (
        policy["max_replacement_character_warning_ratio"]
        < replacement_ratio
        <= policy["max_replacement_character_ratio"]
    ):
        issues.append(
            _metric_issue(
                "PDF_REPLACEMENT_CHARACTERS",
                "warning",
                replacement_ratio,
                policy["max_replacement_character_warning_ratio"],
            )
        )
    elif replacement_ratio > policy["max_replacement_character_ratio"]:
        issues.append(
            _metric_issue(
                "PDF_TEXT_ENCODING_UNUSABLE",
                "incomplete",
                replacement_ratio,
                policy["max_replacement_character_ratio"],
            )
        )
    if (
        metrics["rotated_body_character_ratio"]
        > policy["max_rotated_body_character_ratio"]
    ):
        issues.append(
            _metric_issue(
                "PDF_ROTATED_TEXT_UNSUPPORTED",
                "unsupported",
                metrics["rotated_body_character_ratio"],
                policy["max_rotated_body_character_ratio"],
            )
        )
    if metrics.get("rotated_pages"):
        pages = metrics["rotated_pages"]
        issues.append(
            {
                "issue_id": stable_id("issue", "PDF_ROTATED_PAGE_UNRESOLVED", *pages),
                "severity": "incomplete",
                "code": "PDF_ROTATED_PAGE_UNRESOLVED",
                "message": f"Rotated pages require explicit coordinate normalization review: {pages}.",
                "block_ids": [],
                "provenance": [],
                "asset_refs": [],
            }
        )
    if (
        metrics["one_or_two_column_page_ratio"]
        < policy["min_one_or_two_column_page_ratio"]
    ):
        issues.append(
            _metric_issue(
                "PDF_LAYOUT_UNSUPPORTED",
                "unsupported",
                metrics["one_or_two_column_page_ratio"],
                policy["min_one_or_two_column_page_ratio"],
            )
        )
    if metrics.get("ambiguous_layout_pages"):
        pages = metrics["ambiguous_layout_pages"]
        issues.append(
            {
                "issue_id": stable_id("issue", "PDF_LAYOUT_AMBIGUOUS", *pages),
                "severity": "incomplete",
                "code": "PDF_LAYOUT_AMBIGUOUS",
                "message": f"Reading order is ambiguous on pages {pages}.",
                "block_ids": [],
                "provenance": [],
                "asset_refs": [],
            }
        )
    if (
        metrics["visible_ink_component_accounting_ratio"]
        < policy["min_visible_ink_accounting_ratio"]
    ):
        issues.append(
            _metric_issue(
                "PDF_VISIBLE_INK_UNACCOUNTED",
                "incomplete",
                metrics["visible_ink_component_accounting_ratio"],
                policy["min_visible_ink_accounting_ratio"],
            )
        )

    severity = {"warning": 1, "incomplete": 2, "unsupported": 3, "fatal": 4}
    highest = max((severity[item["severity"]] for item in issues), default=0)
    status = {
        0: "complete",
        1: "complete_with_warnings",
        2: "incomplete",
        3: "unsupported",
        4: "fatal",
    }[highest]
    kind_counts = Counter(block.kind for block in blocks)
    verified_tables = sum(
        1
        for block in blocks
        if block.kind == "table" and block.status == "ok" and block.table and block.table.get("structure_verified")
    )
    verified_figures = sum(1 for block in blocks if block.kind == "figure" and block.status == "ok")
    qa_metrics = {
        **metrics,
        "figures": kind_counts["figure"] + kind_counts["figure_caption"],
        "tables": kind_counts["table"] + kind_counts["table_caption"],
        "equations": kind_counts["equation"],
        "verified_equations": sum(
            block.kind == "equation"
            and bool(block.equation)
            and bool(block.equation.get("latex_verified"))
            for block in blocks
        ),
        "verified_tables": verified_tables,
        "verified_figures": verified_figures,
        "unresolved_blocks": len(unresolved),
    }
    rotated_pages = set(metrics.get("rotated_pages", []))
    page_candidates = [
        {
            "page": int(page),
            "reason": "native_text_below_100_valid_characters",
            "valid_characters": counts["valid"],
            "invalid_characters": counts["invalid"],
        }
        for page, counts in metrics.get("native_text_by_page", {}).items()
        if counts["valid"] < 100 and int(page) not in rotated_pages
    ]
    block_candidates = [
        {
            "block_id": block.block_id,
            "page": block.provenance[0]["page"],
            "bbox": block.provenance[0]["bbox"],
            "reason": "unmapped_native_glyph",
        }
        for block in blocks
        if "PDF_GLYPH_UNRESOLVED" in block.issues
    ]
    return {
        "schema_version": 1,
        "source_sha256": source_sha256,
        "run_id": run_id,
        "materialization_id": materialization_id,
        "policy": {"name": policy["name"], "sha256": policy_sha256},
        "status": status,
        "metrics": qa_metrics,
        "ocr_candidates": {"pages": page_candidates, "blocks": block_candidates},
        "issues": sorted(issues, key=lambda item: (item["severity"], item["code"], item["issue_id"])),
    }


def render_qa_markdown(qa: dict[str, Any]) -> str:
    lines = [
        "# PDF import QA",
        "",
        f"Status: **{qa['status']}**",
        "",
        "## Metrics",
        "",
    ]
    for name, value in sorted(qa["metrics"].items()):
        lines.append(f"- `{name}`: `{value}`")
    lines.extend(["", "## Issues", ""])
    if not qa["issues"]:
        lines.append("No issues.")
    for issue in qa["issues"]:
        location = ""
        if issue["provenance"]:
            item = issue["provenance"][0]
            location = f" (page {item['page']}, bbox {item['bbox']})"
        lines.append(
            f"- **{issue['severity']}** `{issue['code']}`{location}: {issue['message']}"
        )
    return "\n".join(lines) + "\n"


def _issue(code: str, severity: str, block: PdfBlock) -> dict[str, Any]:
    return {
        "issue_id": stable_id("issue", code, block.block_id),
        "severity": severity,
        "code": code,
        "message": _message(code),
        "block_ids": [block.block_id],
        "provenance": block.provenance,
        "asset_refs": block.asset_refs,
    }


def _metric_issue(code: str, severity: str, actual: float, threshold: float) -> dict[str, Any]:
    return {
        "issue_id": stable_id("issue", code, f"{actual:.8f}", f"{threshold:.8f}"),
        "severity": severity,
        "code": code,
        "message": f"Measured {actual:.4%}; policy threshold is {threshold:.4%}.",
        "block_ids": [],
        "provenance": [],
        "asset_refs": [],
    }


def _message(code: str) -> str:
    return {
        "PDF_VISIBLE_REGION_UNRESOLVED": "Visible non-text content requires element recovery.",
        "PDF_FIGURE_UNRESOLVED": "Figure caption was found but the figure is not yet structured.",
        "PDF_TABLE_UNRESOLVED": "Table caption was found but the table is not yet structured.",
        "PDF_EQUATION_UNRESOLVED": "Display equation requires structure recovery.",
        "PDF_GLYPH_UNRESOLVED": "Text contains glyphs without a trustworthy Unicode mapping.",
        "PDF_HEADING_AMBIGUOUS": "A bold short line may be a section heading and needs review.",
        "PDF_DEHYPHENATION_AMBIGUOUS": "A line-end hyphen cannot be removed without lexical evidence.",
        "PDF_CROSS_PAGE_CONTINUATION_UNRESOLVED": "A paragraph may continue across a page boundary.",
    }.get(code, "PDF-derived content requires review before segmentation.")
