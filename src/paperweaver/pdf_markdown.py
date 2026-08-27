"""Deterministic views over canonical PDF blocks."""

from __future__ import annotations

import html
import re
from typing import Any

from .models import PdfArticleMap, PdfBlock
from .pdf_contracts import stable_id


def materialize_markdown(
    blocks: list[PdfBlock],
    materialization_id: str,
    asset_paths: dict[str, str] | None = None,
    slot_values: dict[str, str] | None = None,
) -> tuple[str, list[PdfArticleMap], dict[str, Any]]:
    asset_paths = asset_paths or {}
    slot_values = slot_values or {}
    lines: list[str] = []
    mappings: list[PdfArticleMap] = []
    nodes: list[dict[str, Any]] = []
    rendered_title = False
    ordered = sorted(blocks, key=lambda item: item.ordinal)
    titles = [item for item in ordered if item.kind == "document_title"]
    metadata = [item for item in ordered if item.kind == "metadata"]
    ordered = titles[:1] + metadata + [
        item for item in ordered if item not in titles[:1] and item not in metadata
    ]
    for block in ordered:
        if block.disposition == "excluded_artifact":
            continue
        anchor_line = len(lines) + 1
        lines.append(f"<!-- paperweaver:block {block.block_id} -->")
        content_start = len(lines) + 1
        node_id = stable_id("node", materialization_id, block.block_id)
        slots = _slots_for_block(block, materialization_id, node_id)
        rendered = _render_block(
            block, rendered_title, asset_paths, slots, slot_values
        )
        if block.kind == "document_title":
            rendered_title = True
        lines.extend(rendered)
        content_end = len(lines)
        lines.append("")
        mappings.append(
            PdfArticleMap(
                1,
                node_id,
                block.block_id,
                anchor_line,
                content_start,
                content_end,
                [block.block_id],
            )
        )
        nodes.append(
            {
                "node_id": node_id,
                "type": _node_type(block),
                "block_ids": [block.block_id],
                "children": [],
                "slots": slots,
            }
        )
    if not rendered_title:
        lines[0:0] = [
            "<!-- paperweaver:system-slot unresolved-title -->",
            "# [Unresolved document title - see QA]",
            "",
        ]
        shift = 3
        mappings = [
            PdfArticleMap(
                item.schema_version,
                item.render_node_id,
                item.anchor_block_id,
                item.markdown_anchor_line + shift,
                item.content_start_line + shift,
                item.content_end_line + shift,
                item.block_ids,
            )
            for item in mappings
        ]
    markdown = "\n".join(lines).rstrip() + "\n"
    tree = {
        "schema_version": 1,
        "materialization_id": materialization_id,
        "type": "document",
        "nodes": nodes,
    }
    return markdown, mappings, tree


def _render_block(
    block: PdfBlock,
    rendered_title: bool,
    asset_paths: dict[str, str],
    slots: list[dict[str, Any]],
    slot_values: dict[str, str],
) -> list[str]:
    text = _slot_text(slots[0], slot_values) if slots else block.text or ""
    if block.status == "unresolved":
        page = block.provenance[0]["page"]
        issue = block.issues[0] if block.issues else "PDF_BLOCK_UNRESOLVED"
        rendered = [
            "> [!WARNING]",
            f"> Unresolved PDF content at page {page}. See QA issue `{issue}`.",
            "",
            text,
        ]
        for asset_ref in block.asset_refs:
            if asset_ref in asset_paths:
                rendered.extend(["", f"![Unresolved content on page {page}]({asset_paths[asset_ref]})"])
        return rendered
    if block.kind == "document_title":
        return [f"# {text}" if not rendered_title else f"## {text}"]
    if block.kind == "section_heading":
        return [f"{'#' * _heading_level(text)} {text}"]
    if block.kind == "equation":
        return _render_equation(block)
    if block.kind == "table":
        return _render_table(block, slots, slot_values)
    if block.kind == "figure":
        return _render_figure(block, asset_paths)
    if block.kind == "reference":
        entries = _reference_entries(text)
        return [value for entry in entries for value in (entry, "")][:-1]
    return [text]


def _render_table(
    block: PdfBlock, slots: list[dict[str, Any]], slot_values: dict[str, str]
) -> list[str]:
    payload = block.table
    if not payload or not payload.get("rows"):
        return [block.text or ""]
    rows = [list(row) for row in payload["rows"]]
    for slot in slots:
        locator = slot["sub_locator"]
        if locator is None:
            continue
        rows[locator["row"]][locator["column"]] = _slot_text(slot, slot_values)
    header_rows = int(payload.get("header_rows", 0))
    row_spans = payload.get("row_spans", [])
    col_spans = payload.get("col_spans", [])
    if row_spans or col_spans:
        return _html_table(rows, header_rows, row_spans, col_spans)
    return _pipe_table(rows, header_rows)


def _pipe_table(rows: list[list[str]], header_rows: int) -> list[str]:
    out: list[str] = []
    for index, row in enumerate(rows):
        cells = [_escape_pipe(cell) for cell in row]
        out.append("| " + " | ".join(cells) + " |")
        if index == header_rows - 1:
            out.append("| " + " | ".join("---" for _ in row) + " |")
    return out


def _escape_pipe(value: str) -> str:
    return html.escape(value, quote=False).replace("|", r"\|").replace("\n", "<br>")


def _html_table(
    rows: list[list[str]], header_rows: int, row_spans: list[dict], col_spans: list[dict]
) -> list[str]:
    colspan: dict[tuple[int, int], int] = {
        (item["row"], item["column"]): item["span"] for item in col_spans
    }
    rowspan: dict[tuple[int, int], int] = {
        (item["row"], item["column"]): item["span"] for item in row_spans
    }
    out = ["<table>"]
    if header_rows > 0:
        out.append("<thead>")
        for row in range(header_rows):
            cells = _html_cells(rows[row], row, colspan, rowspan, "th")
            out.append("<tr>" + cells + "</tr>")
        out.append("</thead>")
    out.append("<tbody>")
    for row in range(header_rows, len(rows)):
        out.append("<tr>" + _html_cells(rows[row], row, colspan, rowspan, "td") + "</tr>")
    out.append("</tbody>")
    out.append("</table>")
    return out


def _html_cells(
    row: list[str],
    row_index: int,
    colspan: dict[tuple[int, int], int],
    rowspan: dict[tuple[int, int], int],
    tag: str,
) -> str:
    pieces: list[str] = []
    for column, value in enumerate(row):
        attributes = []
        if (row_index, column) in colspan:
            attributes.append(f'colspan="{colspan[(row_index, column)]}"')
        if (row_index, column) in rowspan:
            attributes.append(f'rowspan="{rowspan[(row_index, column)]}"')
        suffix = " " + " ".join(attributes) if attributes else ""
        escaped = html.escape(value).replace("\n", "<br>")
        pieces.append(f"<{tag}{suffix}>{escaped}</{tag}>")
    return "".join(pieces)


def _render_figure(block: PdfBlock, asset_paths: dict[str, str]) -> list[str]:
    label = block.text or "Figure"
    if block.asset_refs and block.asset_refs[0] in asset_paths:
        return [f"![{label}]({asset_paths[block.asset_refs[0]]})"]
    return [f"![{label}]"]


def _render_equation(block: PdfBlock) -> list[str]:
    payload = block.equation or {}
    latex = payload.get("latex")
    if not latex or not payload.get("latex_verified"):
        return [block.text or ""]
    number = payload.get("number")
    rendered = f"{latex} \\tag{{{number}}}" if number else latex
    return ["$$", rendered, "$$"]


def _heading_level(text: str) -> int:
    compact = re.sub(r"\s+", "", text).casefold()
    if compact in {"abstract", "references"}:
        return 2
    match = re.match(r"^(\d+(?:\.\d+)*)\.?\s+", text)
    return min(2 + match.group(1).count("."), 6) if match else 2


def _reference_entries(text: str) -> list[str]:
    boundary = re.compile(
        r"(?<=[^,&] )(?=(?:[A-Za-z][A-Za-z-]*, [A-Z]\.|[A-Z][A-Za-z0-9-]+\. \((?:cited|\d{4})))"
    )
    entries = [item.strip() for item in boundary.split(text) if item.strip()]
    return entries or [text]


def _node_type(block: PdfBlock) -> str:
    return {
        "document_title": "heading",
        "section_heading": "heading",
        "figure_caption": "text",
        "table_caption": "text",
        "unknown": "figure",
        "table": "table",
        "figure": "figure",
        "equation": "equation",
        "reference": "references",
    }.get(block.kind, "text")


def _slots_for_block(
    block: PdfBlock, materialization_id: str, node_id: str
) -> list[dict[str, Any]]:
    if block.kind == "table" and block.table:
        slots = []
        for row_index, row in enumerate(block.table.get("rows", [])):
            for column_index, value in enumerate(row):
                locator = {"row": row_index, "column": column_index}
                slots.append(
                    {
                        "slot_id": stable_id(
                            "slot",
                            materialization_id,
                            node_id,
                            "table_cell",
                            row_index,
                            column_index,
                        ),
                        "role": "table_cell",
                        "mode": "translate" if _translatable_table_cell(value) else "literal",
                        "source_text": value,
                        "sub_locator": locator,
                        "protected_tokens": [],
                    }
                )
        return slots
    literal = {
        "unknown",
        "equation",
        "reference",
        "figure",
        "metadata",
        "document_title",
        "section_heading",
    }
    return [
        {
            "slot_id": stable_id("slot", materialization_id, node_id, "content"),
            "role": block.kind,
            "mode": "literal" if block.kind in literal else "translate",
            "source_text": block.text or "",
            "sub_locator": None,
            "protected_tokens": [],
        }
    ]


def _translatable_table_cell(value: str) -> bool:
    text = value.strip()
    if not text or not any(character.isalpha() for character in text):
        return False
    return not (len(text) <= 2 and text.isalpha() and text.upper() == text)


def _slot_text(slot: dict[str, Any], slot_values: dict[str, str]) -> str:
    return slot_values.get(slot["slot_id"], slot["source_text"])
