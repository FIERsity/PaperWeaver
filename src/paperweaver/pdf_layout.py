"""Deterministic, conservative layout recovery for born-digital journal PDFs."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, replace
from itertools import pairwise
from statistics import median
from typing import Any

from . import pdf_equation, pdf_table, pdf_visual
from .models import PdfBlock, PdfObjectAccounting
from .pdf_backend import PdfBackendRun, PdfPageObservation, RawPdfObject
from .pdf_contracts import make_block_id

KNOWN_HEADINGS = {
    "abstract",
    "introduction",
    "background",
    "methods",
    "methodology",
    "data",
    "results",
    "discussion",
    "conclusion",
    "conclusions",
    "references",
    "acknowledgements",
    "acknowledgments",
    "dataavailability",
    "declarationofcompetinginterest",
    "creditauthorshipcontributionstatement",
}
SIDEBAR_METADATA_LABELS = {
    "software",
    "editor",
    "reviewers",
    "submitted",
    "published",
    "license",
}


@dataclass(frozen=True)
class LayoutLine:
    page: int
    bbox: list[float]
    text: str
    raw_text: str
    object_refs: list[str]
    font_size: float
    fontname: str
    page_width: float
    column: str = "full"
    fragment: bool = False


@dataclass(frozen=True)
class LayoutResult:
    blocks: list[PdfBlock]
    accounting: list[PdfObjectAccounting]
    metrics: dict[str, Any]


def recover_layout(
    run: PdfBackendRun,
    source_sha256: str,
    run_id: str,
    policy: dict[str, Any],
) -> LayoutResult:
    page_lines = {
        page.page: _lines_from_chars(page, policy) for page in run.pages
    }
    fragment_pages: set[int] = set()
    page_symbol_fonts: dict[int, set[str]] = {}
    fragment_initial_fonts: dict[int, set[str]] = {}

    def _span(line: LayoutLine) -> tuple[float, float, float, float]:
        return (
            round(line.bbox[0], 2),
            round(line.bbox[1], 2),
            round(line.bbox[2], 2),
            round(line.bbox[3], 2),
        )

    fragment_spans: dict[int, set[tuple[float, float, float, float]]] = {}
    for page in run.pages:
        symbol_fonts = _symbol_fonts(page, policy)
        if not symbol_fonts:
            continue
        flags = {
            _span(line)
            for line in page_lines[page.page]
            if _is_equation_fragment_line(line, page, symbol_fonts, policy)
        }
        if flags:
            fragment_spans[page.page] = flags
            fragment_pages.add(page.page)
            page_symbol_fonts[page.page] = symbol_fonts
            fragment_initial_fonts[page.page] = symbol_fonts
    document_tokens = _document_tokens(page_lines)
    artifact_keys = _repeated_artifacts(run.pages, page_lines, policy)
    repeated_visual_refs = _repeated_visual_artifact_refs(run.pages, policy)
    blocks: list[PdfBlock] = []
    accounting_by_ref: dict[str, PdfObjectAccounting] = {}
    valid_characters = invalid_characters = rotated_characters = 0
    native_text_by_page: dict[int, dict[str, int]] = {}
    page_column_counts: dict[int, int] = {}

    for page in run.pages:
        equations = pdf_equation.detect_equations(page.objects, page.width)
        native_text_by_page[page.page] = {"valid": 0, "invalid": 0}
        visual_clusters = pdf_visual.cluster_visual_objects(
            page.objects, page.width, page.height
        )
        filtered_visual_clusters = []
        for cluster in visual_clusters:
            retained = [
                item for item in cluster.objects if item.object_ref not in repeated_visual_refs
            ]
            if retained:
                filtered_visual_clusters.append(
                    pdf_visual.VisualCluster(
                        tuple(retained), tuple(_union_bbox([item.bbox for item in retained]))
                    )
                )
        content_clusters = [
            cluster
            for cluster in filtered_visual_clusters
            if not pdf_visual.decorative_cluster(
                cluster, float(policy["rule_thickness_pt"])
            )
        ]
        # ---- Pass 1: detect verified tables so their characters leave the text layer ----
        consumed = _verified_table_chars(
            page, page_lines[page.page], document_tokens, artifact_keys, policy
        )
        figure_chars, claimed_visual_refs = _verified_figure_chars(
            page,
            page_lines[page.page],
            document_tokens,
            artifact_keys,
            content_clusters,
            policy,
            _table_stake_boxes(
                page, page_lines[page.page], document_tokens, artifact_keys, policy
            ),
        )
        consumed.update(figure_chars)
        consumed.update(
            object_ref for equation in equations for object_ref in equation.object_refs
        )
        lines = _lines_from_chars(page, policy, exclude=consumed)
        flagged = fragment_spans.get(page.page, set())
        if page.page in fragment_pages and page.page in page_symbol_fonts:
            extended = page_symbol_fonts[page.page]
            if extended != fragment_initial_fonts.get(page.page):
                flagged = {
                    _span(line)
                    for line in page_lines[page.page]
                    if _is_equation_fragment_line(line, page, extended, policy)
                }
        if flagged:
            lines = [replace(line, fragment=_span(line) in flagged) for line in lines]
        artifact_lines = [line for line in lines if _artifact_key(page, line, policy) in artifact_keys]
        content_lines = [line for line in lines if line not in artifact_lines]
        ordered, columns = _reading_order(page, content_lines, policy)
        page_column_counts[page.page] = columns

        for line in artifact_lines:
            kind = "page_number" if _looks_like_page_number(line.text) else (
                "header" if line.bbox[1] < page.height / 2 else "footer"
            )
            block = _make_block(
                source_sha256,
                run_id,
                len(blocks) + 1,
                kind,
                "ok",
                "excluded_artifact",
                [line],
                page,
                issues=[],
            )
            blocks.append(block)
            for object_ref in line.object_refs:
                accounting_by_ref[object_ref] = PdfObjectAccounting(
                    1,
                    object_ref,
                    "char",
                    "excluded_artifact",
                    None,
                    [],
                    None,
                    f"PDF_{kind.upper()}",
                )

        page_fragments = {id(line) for line in ordered if line.fragment}
        paragraphs = _paragraphs(ordered, page_fragments)
        if page.page in fragment_pages:
            extended = _extend_symbol_fonts(
                page, page_symbol_fonts[page.page], paragraphs, policy
            )
            if extended != page_symbol_fonts[page.page]:
                page_symbol_fonts[page.page] = extended
                page_fragments = {
                    id(line)
                    for line in ordered
                    if _is_equation_fragment_line(line, page, extended, policy)
                }
                paragraphs = _paragraphs(ordered, page_fragments)
        title_index = (
            _document_title_index(page, paragraphs, document_tokens)
            if page.page == 1
            else None
        )
        page_para_start = len(blocks)
        for paragraph_index, paragraph in enumerate(paragraphs):
            text, ambiguous_hyphen = _join_paragraph_lines(paragraph, document_tokens)
            kind, status, issues = _classify_block(
                text, paragraph_index == title_index, paragraph, ambiguous_hyphen
            )
            if all(id(line) in page_fragments for line in paragraph):
                kind, status, issues = "equation", "unresolved", ["PDF_EQUATION_UNRESOLVED"]
            disposition = "render" if status != "unresolved" else "unresolved_placeholder"
            block = _make_block(
                source_sha256,
                run_id,
                len(blocks) + 1,
                kind,
                status,
                disposition,
                paragraph,
                page,
                issues=issues,
                text=text,
            )
            if status == "unresolved" and kind == "equation":
                block = replace(
                    block, equation={"latex": None, "number": None, "latex_verified": False}
                )
            blocks.append(block)
            primary = "unresolved" if status == "unresolved" else "rendered"
            for line in paragraph:
                for object_ref in line.object_refs:
                    accounting_by_ref[object_ref] = PdfObjectAccounting(
                        1, object_ref, "char", primary, block.block_id, [], None, None
                    )

        # ---- Pass 2: turn captions into table/figure blocks ----
        elements, updated, claims, placements = _build_elements(
            blocks[page_para_start:],
            page,
            page.objects,
            source_sha256,
            run_id,
            policy,
            len(blocks) + 1,
            content_clusters,
            claimed_visual_refs,
        )
        page_blocks: list[PdfBlock] = []
        for candidate in blocks[page_para_start:]:
            effective = updated.get(candidate.ordinal, candidate)
            placement = placements.get(candidate.ordinal)
            if placement is not None and placement[0] == "before":
                page_blocks.append(placement[1])
            page_blocks.append(effective)
            if placement is not None and placement[0] == "after":
                page_blocks.append(placement[1])
        placed_ids = {item[1].block_id for item in placements.values()}
        page_blocks.extend(item for item in elements if item.block_id not in placed_ids)
        for candidate in equations:
            verified = candidate.verified and not _equation_visual_overlap(
                candidate, page.objects, policy
            )
            line = _element_line(
                page,
                list(candidate.bbox),
                candidate.raw_text,
                list(candidate.object_refs),
            )
            equation_block = replace(
                _make_block(
                    source_sha256,
                    run_id,
                    len(blocks) + len(page_blocks) + 1,
                    "equation",
                    "ok" if verified else "unresolved",
                    "render" if verified else "unresolved_placeholder",
                    [line],
                    page,
                    issues=[] if verified else ["PDF_EQUATION_UNRESOLVED"],
                    text=candidate.raw_text,
                ),
                equation={
                    "latex": candidate.latex if verified else None,
                    "number": candidate.number,
                    "latex_verified": verified,
                },
            )
            position = next(
                (
                    index
                    for index, block in enumerate(page_blocks)
                    if block.provenance[0]["bbox"][1] > candidate.bbox[1]
                ),
                len(page_blocks),
            )
            page_blocks.insert(position, equation_block)
            for object_ref in candidate.object_refs:
                claims[object_ref] = (
                    "rendered" if verified else "unresolved",
                    equation_block.block_id,
                    None if verified else "PDF_EQUATION_UNRESOLVED",
                )
        blocks[page_para_start:] = page_blocks
        for object_ref, (disposition, block_id, reason) in claims.items():
            kind_of = next(
                (item.kind for item in page.objects if item.object_ref == object_ref), "char"
            )
            accounting_by_ref[object_ref] = PdfObjectAccounting(
                1, object_ref, kind_of, disposition, block_id, [], None, reason
            )

        for cluster in visual_clusters:
            repeated = [
                item for item in cluster.objects if item.object_ref in repeated_visual_refs
            ]
            for item in repeated:
                accounting_by_ref[item.object_ref] = PdfObjectAccounting(
                    1,
                    item.object_ref,
                    item.kind,
                    "excluded_artifact",
                    None,
                    [],
                    None,
                    "PDF_REPEATED_VISUAL_HEADER_FOOTER",
                )
            remaining = [
                item
                for item in cluster.objects
                if item.object_ref not in claims
                and item.object_ref not in repeated_visual_refs
            ]
            if not remaining:
                continue
            remaining_cluster = pdf_visual.VisualCluster(
                tuple(remaining), tuple(_union_bbox([item.bbox for item in remaining]))
            )
            if pdf_visual.decorative_cluster(
                remaining_cluster, float(policy["rule_thickness_pt"])
            ) or _link_decoration_cluster(remaining_cluster, page.objects):
                for item in remaining:
                    accounting_by_ref[item.object_ref] = PdfObjectAccounting(
                        1,
                        item.object_ref,
                        item.kind,
                        "excluded_artifact",
                        None,
                        [],
                        None,
                        (
                            "PDF_LINK_DECORATION"
                            if _link_decoration_cluster(remaining_cluster, page.objects)
                            else "PDF_DECORATIVE_RULE"
                        ),
                    )
                continue
            visual_line = LayoutLine(
                page.page,
                list(remaining_cluster.bbox),
                f"Unresolved visual content on page {page.page}",
                f"Unresolved visual content on page {page.page}",
                [item.object_ref for item in remaining],
                0.0,
                "",
                page.width,
            )
            visual_block = _make_block(
                source_sha256,
                run_id,
                len(blocks) + 1,
                "unknown",
                "unresolved",
                "unresolved_placeholder",
                [visual_line],
                page,
                issues=["PDF_VISIBLE_REGION_UNRESOLVED"],
                text=f"Unresolved visual content on page {page.page}",
            )
            blocks.append(visual_block)
            for item in remaining:
                accounting_by_ref[item.object_ref] = PdfObjectAccounting(
                    1,
                    item.object_ref,
                    item.kind,
                    "unresolved",
                    visual_block.block_id,
                    [],
                    None,
                    None,
                )
        for item in (item for item in page.objects if item.kind == "annotation"):
            accounting_by_ref[item.object_ref] = PdfObjectAccounting(
                1,
                item.object_ref,
                item.kind,
                "excluded_artifact",
                None,
                [],
                None,
                "PDF_DECORATIVE_RULE",
            )

        for item in page.objects:
            if item.kind != "char":
                continue
            value = item.payload
            if value.isspace():
                if item.object_ref not in accounting_by_ref:
                    accounting_by_ref[item.object_ref] = PdfObjectAccounting(
                        1,
                        item.object_ref,
                        "char",
                        "excluded_artifact",
                        None,
                        [],
                        None,
                        "PDF_WHITESPACE",
                    )
                continue
            if _valid_unicode(value):
                valid_characters += 1
                native_text_by_page[page.page]["valid"] += 1
            else:
                invalid_characters += 1
                native_text_by_page[page.page]["invalid"] += 1
            if page.rotation in {90, 270} or not item.attrs.get("upright", True):
                rotated_characters += 1

    blocks, merged_block_ids = _merge_cross_page_continuations(blocks)
    if merged_block_ids:
        accounting_by_ref = {
            object_ref: replace(
                item,
                primary_block_id=_final_block_id(item.primary_block_id, merged_block_ids),
                supporting_block_ids=[
                    _final_block_id(block_id, merged_block_ids) or block_id
                    for block_id in item.supporting_block_ids
                ],
            )
            if item.primary_block_id in merged_block_ids
            or any(block_id in merged_block_ids for block_id in item.supporting_block_ids)
            else item
            for object_ref, item in accounting_by_ref.items()
        }
    blocks = [replace(block, ordinal=index) for index, block in enumerate(blocks, 1)]
    blocks = _mark_front_matter(blocks)
    blocks = _mark_reference_blocks(blocks)
    blocks, cross_page_blocks = _mark_cross_page_ambiguities(blocks)
    if cross_page_blocks:
        accounting_by_ref = {
            object_ref: replace(item, primary_disposition="unresolved")
            if item.primary_block_id in cross_page_blocks
            else item
            for object_ref, item in accounting_by_ref.items()
        }
    missing = [
        item.object_ref
        for page in run.pages
        for item in page.objects
        if item.object_ref not in accounting_by_ref
    ]
    if missing:
        raise RuntimeError(
            f"PDF_OBJECT_ACCOUNTING_INCOMPLETE: {len(missing)} objects have no disposition"
        )

    total_characters = valid_characters + invalid_characters
    usable_pages = sum(
        1
        for page in run.pages
        if sum(1 for item in page.objects if item.kind == "char" and _valid_unicode(item.payload))
        >= 100
    )
    metrics = {
        "pages": len(run.pages),
        "leaf_objects": sum(len(page.objects) for page in run.pages),
        "valid_unicode_ratio": valid_characters / total_characters if total_characters else 0.0,
        "replacement_character_ratio": (
            invalid_characters / total_characters if total_characters else 1.0
        ),
        "unresolved_glyphs": invalid_characters,
        "rotated_body_character_ratio": (
            rotated_characters / total_characters if total_characters else 0.0
        ),
        "rotated_pages": [page.page for page in run.pages if page.rotation != 0],
        "content_pages_with_usable_text_ratio": usable_pages / len(run.pages),
        "one_or_two_column_page_ratio": (
            sum(count in {1, 2} for count in page_column_counts.values()) / len(run.pages)
        ),
        "source_object_accounting_ratio": 1.0,
        "columns_by_page": page_column_counts,
        "ambiguous_layout_pages": [
            page for page, count in page_column_counts.items() if count not in {1, 2}
        ],
        "native_text_by_page": native_text_by_page,
    }
    return LayoutResult(blocks, list(accounting_by_ref.values()), metrics)


def _lines_from_chars(
    page: PdfPageObservation, policy: dict[str, Any], exclude: set[str] | None = None
) -> list[LayoutLine]:
    exclude = exclude or set()
    chars = [
        item
        for item in page.objects
        if item.kind == "char" and item.payload and item.object_ref not in exclude
    ]
    chars.sort(key=lambda item: (item.bbox[1], item.bbox[0]))
    baselines: list[list[RawPdfObject]] = []
    tolerance = float(policy["line_y_tolerance_points"])
    for char in chars:
        target = next(
            (
                row
                for row in reversed(baselines[-4:])
                if abs(median(item.bbox[1] for item in row) - char.bbox[1]) <= tolerance
            ),
            None,
        )
        if target is None:
            baselines.append([char])
        else:
            target.append(char)

    lines: list[LayoutLine] = []
    split_gap = float(policy["line_split_gap_points"])
    for baseline in baselines:
        ordered = sorted(baseline, key=lambda item: item.bbox[0])
        segments: list[list[RawPdfObject]] = [[]]
        previous: RawPdfObject | None = None
        for char in ordered:
            if previous is not None and char.bbox[0] - previous.bbox[2] > split_gap:
                segments.append([])
            segments[-1].append(char)
            previous = char
        for segment in segments:
            text = _join_chars(segment).strip()
            raw_text = "".join(item.payload for item in segment).strip()
            if not text:
                continue
            sizes = [float(item.attrs.get("size", 0.0)) for item in segment]
            fonts = [str(item.attrs.get("fontname", "")) for item in segment]
            lines.append(
                LayoutLine(
                    page.page,
                    _union_bbox([item.bbox for item in segment]),
                    text,
                    raw_text,
                    [item.object_ref for item in segment],
                    median(sizes) if sizes else 0.0,
                    max(set(fonts), key=fonts.count) if fonts else "",
                    page.width,
                )
            )
    return sorted(lines, key=lambda line: (line.bbox[1], line.bbox[0]))


def _join_chars(chars: list[RawPdfObject]) -> str:
    output = ""
    previous: RawPdfObject | None = None
    for char in chars:
        value = char.payload
        if previous is not None and value != " " and not output.endswith(" "):
            gap = char.bbox[0] - previous.bbox[2]
            size = max(float(previous.attrs.get("size", 0.0)), 1.0)
            if gap > max(0.8, size * 0.18):
                output += " "
        output += value
        previous = char
    return re.sub(r"\s+([,.;:!?%)\]])", r"\1", re.sub(r"\s+", " ", output))


def _repeated_artifacts(
    pages: list[PdfPageObservation],
    lines_by_page: dict[int, list[LayoutLine]],
    policy: dict[str, Any],
) -> set[str]:
    seen: dict[str, set[int]] = defaultdict(set)
    for page in pages:
        for line in lines_by_page[page.page]:
            key = _artifact_key(page, line, policy)
            if key:
                seen[key].add(page.page)
    required = max(
        int(policy["min_repeated_artifact_pages"]),
        math.ceil(len(pages) * float(policy["repeated_artifact_page_ratio"])),
    )
    repeated = {key for key, page_numbers in seen.items() if len(page_numbers) >= required}
    for page in pages:
        for line in lines_by_page[page.page]:
            if (
                line.bbox[1] > page.height * (1 - policy["header_footer_zone_ratio"])
                and _looks_like_page_number(line.text)
            ):
                repeated.add(_artifact_key(page, line, policy))
    return repeated


def _repeated_visual_artifact_refs(
    pages: list[PdfPageObservation], policy: dict[str, Any]
) -> set[str]:
    seen: dict[tuple[Any, ...], set[int]] = defaultdict(set)
    refs_by_key: dict[tuple[Any, ...], set[str]] = defaultdict(set)
    zone = float(policy["header_footer_zone_ratio"])
    for page in pages:
        for cluster in pdf_visual.cluster_visual_objects(
            page.objects, page.width, page.height
        ):
            x0, y0, x1, y1 = cluster.bbox
            near_edge = y0 <= page.height * zone * 1.6 or y1 >= page.height * (1 - zone * 1.6)
            compact = y1 - y0 <= page.height * 0.16
            if not near_edge or not compact:
                continue
            kinds = tuple(sorted(item.kind for item in cluster.objects))
            key = (
                round(x0 / page.width, 2),
                round(y0 / page.height, 2),
                round(x1 / page.width, 2),
                round(y1 / page.height, 2),
                kinds,
            )
            seen[key].add(page.page)
            refs_by_key[key].update(item.object_ref for item in cluster.objects)
    required = max(
        int(policy["min_repeated_artifact_pages"]),
        math.ceil(len(pages) * float(policy["repeated_artifact_page_ratio"])),
    )
    return {
        object_ref
        for key, page_numbers in seen.items()
        if len(page_numbers) >= required
        for object_ref in refs_by_key[key]
    }


def _link_decoration_cluster(
    cluster: pdf_visual.VisualCluster, page_objects: list[RawPdfObject]
) -> bool:
    x0, y0, x1, y1 = cluster.bbox
    if x1 - x0 > 8.0 or y1 - y0 > 8.0:
        return False
    return any(
        item.kind == "annotation"
        and item.bbox[0] - 6.0 <= x0 <= item.bbox[2] + 6.0
        and item.bbox[1] - 3.0 <= y0 <= item.bbox[3] + 3.0
        for item in page_objects
    )


def _artifact_key(
    page: PdfPageObservation, line: LayoutLine, policy: dict[str, Any]
) -> str:
    zone = float(policy["header_footer_zone_ratio"])
    if line.bbox[1] > page.height * zone and line.bbox[3] < page.height * (1 - zone):
        return ""
    normalised = re.sub(r"\d+", "#", re.sub(r"\s+", "", line.text.casefold()))
    return normalised if normalised else ""


def _reading_order(
    page: PdfPageObservation,
    lines: list[LayoutLine],
    policy: dict[str, Any],
) -> tuple[list[LayoutLine], int]:
    by_top: list[list[LayoutLine]] = []
    tolerance = float(policy["line_y_tolerance_points"])
    for line in sorted(lines, key=lambda item: (item.bbox[1], item.bbox[0])):
        target = next(
            (
                row
                for row in reversed(by_top[-3:])
                if abs(median(item.bbox[1] for item in row) - line.bbox[1]) <= tolerance
            ),
            None,
        )
        if target is None:
            by_top.append([line])
        else:
            target.append(line)
    pairs: list[tuple[float, float, list[LayoutLine]]] = []
    min_gap = max(float(policy["line_split_gap_points"]), page.width * policy["column_gap_ratio"])
    three_column_rows: list[list[LayoutLine]] = []
    for row in by_top:
        row = sorted(row, key=lambda item: item.bbox[0])
        if len(row) >= 3 and all(
            right.bbox[0] - left.bbox[2] >= min_gap
            for left, right in pairwise(row)
        ) and all(
            len(item.text) >= 12 or item.bbox[2] - item.bbox[0] >= page.width * 0.15
            for item in row[:3]
        ):
            three_column_rows.append(row)
        if len(row) != 2:
            continue
        gap = row[1].bbox[0] - row[0].bbox[2]
        strong_sides = all(
            len(item.text) >= 20 or item.bbox[2] - item.bbox[0] >= page.width * 0.18
            for item in row
        )
        if gap >= min_gap and strong_sides:
            pairs.append(((row[0].bbox[2] + row[1].bbox[0]) / 2, row[0].bbox[1], row))
    cut: float | None = None
    if len(pairs) < int(policy["column_min_shared_lines"]):
        if (
            len(three_column_rows) >= int(policy["column_min_shared_lines"])
            and not any(
                re.match(r"^table\s+\d+\b", line.text, re.IGNORECASE)
                for line in lines
            )
        ):
            return sorted(lines, key=lambda item: (item.bbox[1], item.bbox[0])), 3
        cut = _inferred_vertical_gutter(lines, page.width)
        if cut is None:
            channel = _channel_gutter(page, lines, policy)
            if channel is not None:
                lines = _split_fused_lines(page, lines, channel, policy)
                cut = (channel[0] + channel[1]) / 2
        if cut is None:
            return sorted(lines, key=lambda item: (item.bbox[1], item.bbox[0])), 1

    cut = cut if cut is not None else median(item[0] for item in pairs)
    left: list[LayoutLine] = []
    right: list[LayoutLine] = []
    spanning: list[LayoutLine] = []
    for line in lines:
        heading_separator = _heading_candidate_line(line) and not any(
            other is not line
            and other.bbox[0] >= cut - tolerance
            and other.bbox[1] <= line.bbox[3] + tolerance
            and line.bbox[1] <= other.bbox[3] + tolerance
            for other in lines
        )
        if heading_separator:
            spanning.append(line)
        elif line.bbox[2] <= cut + tolerance:
            left.append(line)
        elif line.bbox[0] >= cut - tolerance:
            right.append(line)
        else:
            spanning.append(line)
    if not left or not right:
        return sorted(lines, key=lambda item: (item.bbox[1], item.bbox[0])), 1
    return _banded_column_order(left, right, spanning), 2


def _inferred_vertical_gutter(lines: list[LayoutLine], page_width: float) -> float | None:
    step = max(2.0, page_width / 150.0)
    start, stop = page_width * 0.12, page_width * 0.88
    candidates = [start + index * step for index in range(round((stop - start) / step) + 1)]
    scored: list[tuple[float, float]] = []
    for cut in candidates:
        left = [item for item in lines if item.bbox[2] <= cut - 2.0]
        right = [item for item in lines if item.bbox[0] >= cut + 2.0]
        crossing = [item for item in lines if item not in left and item not in right]
        if min(len(left), len(right)) < 3:
            continue
        if len(crossing) > max(3, round((len(left) + len(right)) * 0.15)):
            continue
        left_width = median(item.bbox[2] - item.bbox[0] for item in left)
        right_width = median(item.bbox[2] - item.bbox[0] for item in right)
        asymmetric_sidebar = (
            left_width <= page_width * 0.28 and right_width >= page_width * 0.45
        ) or (
            right_width <= page_width * 0.28 and left_width >= page_width * 0.45
        )
        if not asymmetric_sidebar:
            continue
        score = (
            min(len(left), len(right)) * 3
            + len(left)
            + len(right)
            - len(crossing) * 5
        )
        scored.append((score, cut))
    return max(scored)[1] if scored else None


def _char_gaps(page: PdfPageObservation, line: LayoutLine) -> list[tuple[float, float]]:
    """In-line whitespace gaps as ``(center, width)``, from source-grounded chars."""
    by_ref = {item.object_ref: item for item in page.objects if item.kind == "char"}
    chars = sorted(
        (by_ref[ref] for ref in line.object_refs if ref in by_ref),
        key=lambda item: item.bbox[0],
    )
    return [
        ((left.bbox[2] + right.bbox[0]) / 2, right.bbox[0] - left.bbox[2])
        for left, right in pairwise(chars)
    ]


def _edge_clusters(lines: list[LayoutLine], right: bool, page_width: float) -> list[tuple[float, int]]:
    """``(center, count)`` clusters of one side's strong line edges.

    Only strong lines participate (same rule as pair detection: at least 20
    characters or 18% of the page width), so short marginalia never grounds a
    cluster.
    """
    tolerance = max(1.0, page_width / 600.0)
    edges: list[float] = []
    for item in lines:
        if not (
            len(item.text) >= 20 or item.bbox[2] - item.bbox[0] >= page_width * 0.18
        ):
            continue
        edges.append(item.bbox[2] if right else item.bbox[0])
    edges.sort()
    clusters: list[tuple[float, int]] = []
    for edge in edges:
        if clusters and edge - clusters[-1][0] <= tolerance:
            center, count = clusters.pop()
            clusters.append(((center + edge) / 2, count + 1))
        else:
            clusters.append((edge, 1))
    return clusters


def _channel_gutter(
    page: PdfPageObservation,
    lines: list[LayoutLine],
    policy: dict[str, Any],
) -> tuple[float, float] | None:
    """Narrow interior whitespace channel of a fused two-column page.

    Evidence: co-located clusters of strong left-column right edges and
    right-column left edges, separated by a gap too narrow for pair detection
    (below ``min_gap``) but wide enough to read as a real gutter. Fused body
    lines crossing the candidate channel with an in-line gap inside it credit
    the channel (they are gutter-fused body lines, not spanning elements, so
    legitimate full-width title/abstract/citation bands above the body do not
    mask the channel). Runs only after ``_inferred_vertical_gutter`` has
    declined, so already-working layouts are untouched.
    """
    min_gap = max(float(policy["line_split_gap_points"]), page.width * policy["column_gap_ratio"])
    channel_gap = float(policy["column_channel_min_gap_points"])
    minimum = int(policy["column_min_shared_lines"])
    tolerance = float(policy["line_y_tolerance_points"])
    gaps_by_line = {id(line): _char_gaps(page, line) for line in lines}
    scored: list[tuple[float, tuple[float, float]]] = []
    for xr, left_count in _edge_clusters(lines, True, page.width):
        for xl, right_count in _edge_clusters(lines, False, page.width):
            gap = xl - xr
            if not channel_gap <= gap <= min_gap:
                continue
            center = (xr + xl) / 2
            if not page.width * 0.2 <= center <= page.width * 0.8:
                continue
            window = max(2.0, (xl - xr) / 4)
            splittable = sum(
                1
                for line in lines
                if line.bbox[0] < xr - tolerance
                and line.bbox[2] > xl + tolerance
                and any(
                    width >= channel_gap and center - window <= gap_center <= center + window
                    for gap_center, width in gaps_by_line[id(line)]
                )
            )
            if min(left_count, right_count) < minimum:
                continue
            score = min(left_count, right_count) * 3 + left_count + right_count + splittable * 2
            scored.append((score, (xr, xl)))
    return max(scored)[1] if scored else None


def _split_fused_lines(
    page: PdfPageObservation,
    lines: list[LayoutLine],
    channel: tuple[float, float],
    policy: dict[str, Any],
) -> list[LayoutLine]:
    """Split gutter-fused lines at their widest in-line gap inside the channel.

    Each produced half keeps the source line's flags (``fragment`` etc.)
    through ``dataclasses.replace``. Lines without a qualifying gap (genuine
    spanning elements such as title, abstract or citation lines) are returned
    unchanged, as are halves that would be trivial.
    """
    xr, xl = channel
    cut = (xr + xl) / 2
    window = max(2.0, (xl - xr) / 4)
    channel_gap = float(policy["column_channel_min_gap_points"])
    tolerance = float(policy["line_y_tolerance_points"])
    by_ref = {item.object_ref: item for item in page.objects if item.kind == "char"}
    output: list[LayoutLine] = []
    for line in lines:
        if not (line.bbox[0] < xr - tolerance and line.bbox[2] > xl + tolerance):
            output.append(line)
            continue
        chars = sorted(
            (by_ref[ref] for ref in line.object_refs if ref in by_ref),
            key=lambda item: item.bbox[0],
        )
        best_index: int | None = None
        best_distance = float("inf")
        for index, (left, right) in enumerate(pairwise(chars)):
            center = (left.bbox[2] + right.bbox[0]) / 2
            if right.bbox[0] - left.bbox[2] < channel_gap or abs(center - cut) > window:
                continue
            distance = abs(center - cut)
            if distance < best_distance:
                best_distance = distance
                best_index = index + 1
        if best_index is None:
            output.append(line)
            continue
        parts = (chars[:best_index], chars[best_index:])
        if any(
            len(part) < 6
            or max(item.bbox[2] for item in part) - min(item.bbox[0] for item in part)
            < page.width * 0.04
            for part in parts
        ):
            output.append(line)
            continue
        for part in parts:
            bbox = [
                min(item.bbox[0] for item in part),
                min(item.bbox[1] for item in part),
                max(item.bbox[2] for item in part),
                max(item.bbox[3] for item in part),
            ]
            output.append(
                replace(
                    line,
                    bbox=bbox,
                    text=_join_chars(part),
                    raw_text="".join(item.payload for item in part).strip(),
                    object_refs=[item.object_ref for item in part],
                )
            )
    return output


def _banded_column_order(
    left: list[LayoutLine],
    right: list[LayoutLine],
    spanning: list[LayoutLine],
) -> list[LayoutLine]:
    """Order local two-column bands separated by source-grounded spanning lines."""
    separators = sorted(spanning, key=lambda item: (item.bbox[1], item.bbox[0]))
    boundaries = [(item.bbox[1] + item.bbox[3]) / 2 for item in separators]
    bands: list[list[LayoutLine]] = [[] for _ in range(len(separators) + 1)]
    for line in left + right:
        center = (line.bbox[1] + line.bbox[3]) / 2
        band_index = sum(boundary < center for boundary in boundaries)
        bands[band_index].append(line)

    output: list[LayoutLine] = []
    for index, band in enumerate(bands):
        band_left = sorted(
            (item for item in band if item in left),
            key=lambda item: (item.bbox[1], item.bbox[0]),
        )
        band_right = sorted(
            (item for item in band if item in right),
            key=lambda item: (item.bbox[1], item.bbox[0]),
        )
        ordered_columns = [(band_left, "left"), (band_right, "right")]
        if band_left and band_right and band_right[0].bbox[1] + 5.0 < band_left[0].bbox[1]:
            ordered_columns.reverse()
        for column_lines, name in ordered_columns:
            output.extend(replace(item, column=name) for item in column_lines)
        if index < len(separators):
            output.append(separators[index])
    return output


MATHY_GLYPHS = set(
    "|(){}[]<>=+−-×⋅·~^_½¼¾ﬀﬁﬂﬃﬄ∂∇∫≈≠≤≥±→←↔"
)


def _symbol_fonts(page: PdfPageObservation, policy: dict[str, Any]) -> set[str]:
    """Fonts whose payload profile is dominated by math glyphs, plus named math fonts.

    Old TeX subsets (AdvP…, CMMI…) carry no italic/math name marks, so the glyph
    profile is the reliable signal: a font whose rendered payloads are mostly
    operators, braces, and overbrace pieces is a math symbol font, whatever its
    name claims. The threshold lives in the policy so it stays auditable.
    """
    threshold = float(policy["math_symbol_font_glyph_ratio"])
    profile: dict[str, list[str]] = {}
    for item in page.objects:
        if item.kind == "char" and item.payload and item.payload.strip():
            profile.setdefault(str(item.attrs.get("fontname", "")), []).append(item.payload)
    fonts: set[str] = set()
    for name, payloads in profile.items():
        marks = ("italic", "math", "stix", "symbol", "cmmi", "cmsy", "cmex", "msam", "msbm")
        if any(mark in name.casefold() for mark in marks):
            fonts.add(name)
            continue
        mathy = sum(1 for value in payloads if value in MATHY_GLYPHS)
        if payloads and mathy / len(payloads) >= threshold:
            fonts.add(name)
    return fonts


def _is_equation_fragment_line(
    line: LayoutLine, page: PdfPageObservation, symbol_fonts: set[str], policy: dict[str, Any]
) -> bool:
    """A visual row built mostly of math-symbol-font glyphs is equation debris."""
    minimum = int(policy["math_fragment_min_chars"])
    share = float(policy["math_fragment_min_share"])
    refs = set(line.object_refs)
    chars = [
        item
        for item in page.objects
        if item.kind == "char"
        and item.payload.strip()
        and item.object_ref in refs
    ]
    if len(chars) < minimum:
        return False
    return sum(1 for item in chars if str(item.attrs.get("fontname", "")) in symbol_fonts) / len(chars) >= share


def _extend_symbol_fonts(
    page: PdfPageObservation,
    symbol_fonts: set[str],
    paragraphs: list[list[LayoutLine]],
    policy: dict[str, Any],
) -> set[str]:
    """Second pass: mid-profile fonts in the rare band below body-font scale.

    Old-TeX equation-letter fonts (italic letters, digits) have a mixed glyph
    profile that the 0.7 profile rule cannot separate from prose italics. They
    are far rarer than the page's body fonts, though, so any font below
    body-font scale (four times ``math_fragment_body_font_share``) whose math
    profile is at least ``math_symbol_font_glyph_ratio`` / 2 joins the symbol
    set. The main body font sits far above that scale, and prose emphasis
    fonts fail the math-profile test.
    """
    share_cap = float(policy["math_fragment_body_font_share"])
    body_share_cap = share_cap * 4  # fonts used broadly across the page are body fonts
    profile: dict[str, list[str]] = {}
    for item in page.objects:
        if item.kind == "char" and item.payload and item.payload.strip():
            profile.setdefault(str(item.attrs.get("fontname", "")), []).append(item.payload)
    total = sum(len(values) for values in profile.values()) or 1
    extended = set(symbol_fonts)
    for name, payloads in profile.items():
        if name in symbol_fonts:
            continue
        mathy = sum(1 for value in payloads if value in MATHY_GLYPHS) / len(payloads)
        share = len(payloads) / total
        if (
            mathy >= float(policy["math_symbol_font_glyph_ratio"]) / 2
            and share < body_share_cap
        ):
            extended.add(name)
    return extended


def _paragraphs(
    lines: list[LayoutLine], fragments: set[int] | None = None
) -> list[list[LayoutLine]]:
    fragments = fragments or set()
    if not lines:
        return []
    body_sizes = [line.font_size for line in lines if line.font_size > 0]
    body_size = median(body_sizes) if body_sizes else 8.0
    paragraphs: list[list[LayoutLine]] = []
    current: list[LayoutLine] = []
    base_x_by_column: dict[str, float] = {}
    for column in {line.column for line in lines}:
        xs = [line.bbox[0] for line in lines if line.column == column]
        if xs:
            base_x_by_column[column] = min(xs)
    for line in lines:
        previous = current[-1] if current else None
        heading = _heading_candidate_line(line)
        previous_heading = previous is not None and _heading_candidate_line(previous)
        fragment = id(line) in fragments
        previous_fragment = previous is not None and id(previous) in fragments
        gap = line.bbox[1] - previous.bbox[3] if previous else 0.0
        indented = line.bbox[0] > base_x_by_column.get(line.column, line.bbox[0]) + body_size
        new = (
            previous is None
            or line.page != previous.page
            or line.column != previous.column
            or heading
            or previous_heading
            or fragment
            or previous_fragment
            or gap > max(5.0, body_size * 0.9)
            or abs(line.font_size - previous.font_size) > max(0.8, body_size * 0.12)
            or (indented and previous.text.rstrip().endswith((".", "?", "!", ":")))
        )
        if new and current:
            paragraphs.append(current)
            current = []
        current.append(line)
    if current:
        paragraphs.append(current)
    return paragraphs


def _document_title_index(
    page: PdfPageObservation,
    paragraphs: list[list[LayoutLine]],
    document_tokens: set[str],
) -> int | None:
    candidates = []
    excluded = ("journal of ", "contents lists", "journal homepage", "science direct")
    for index, paragraph in enumerate(paragraphs):
        text = _join_paragraph_lines(paragraph, document_tokens)[0].casefold()
        top = paragraph[0].bbox[1] / page.height
        caption = re.match(r"^(figure|table)\s+\d+", text)
        if (
            0.04 <= top <= 0.42
            and caption is None
            and not any(value in text for value in excluded)
        ):
            candidates.append((max(line.font_size for line in paragraph), len(text), index))
    return max(candidates)[2] if candidates else None


def _classify_block(
    text: str, is_title: bool, lines: list[LayoutLine], ambiguous_hyphen: bool = False
) -> tuple[str, str, list[str]]:
    if is_title:
        return "document_title", "ok", []
    compact = re.sub(r"\s+", "", text).casefold()
    if _is_heading_line(lines[0]) or compact in {"abstract", "references"}:
        return "section_heading", "ok", []
    if re.search(r"\(cid:\d+\)", text):
        return "paragraph", "unresolved", ["PDF_GLYPH_UNRESOLVED"]
    if re.match(r"^(fig(?:ure)?\.?\s*\d+)", text, re.IGNORECASE):
        return "figure_caption", "unresolved", ["PDF_FIGURE_UNRESOLVED"]
    if re.match(r"^table\s+\d+", text, re.IGNORECASE) and not re.match(
        r"^table\s+\d+\s+(?:reports?|shows?|presents?|summari[sz]es?|compares?|formally|columns?)\b",
        text,
        re.IGNORECASE,
    ):
        return "table_caption", "unresolved", ["PDF_TABLE_UNRESOLVED"]
    if "=" in text and len(text) < 300 and _equation_like_paragraph(lines):
        return "paragraph", "unresolved", ["PDF_EQUATION_UNRESOLVED"]
    if ambiguous_hyphen:
        return "paragraph", "unresolved", ["PDF_DEHYPHENATION_AMBIGUOUS"]
    if _heading_candidate_line(lines[0]):
        label = text.strip().rstrip(":").casefold()
        if label in SIDEBAR_METADATA_LABELS:
            return "metadata", "ok", []
        if _likely_section_heading(text):
            return "section_heading", "ok", []
        if "," in text or re.search(r"\band\b", text, re.IGNORECASE):
            return "paragraph", "ok", []
        return "paragraph", "unresolved", ["PDF_HEADING_AMBIGUOUS"]
    if compact in {"articleinfo", "keywords"}:
        return "metadata", "ok", []
    if (
        lines[0].column == "left"
        and lines[0].page_width > 0
        and max(line.bbox[2] for line in lines) <= lines[0].page_width * 0.27
    ):
        return "metadata", "ok", []
    return "paragraph", "ok", []


def _equation_like_paragraph(lines: list[LayoutLine]) -> bool:
    """Only typeset mathematics justifies flagging a leftover '=' paragraph."""
    weighted = total = 0
    for line in lines:
        weight = max(len(line.raw_text), 1)
        total += weight
        font = line.fontname.casefold()
        if any(mark in font for mark in ("italic", "math", "stix", "symbol")):
            weighted += weight
    return total > 0 and weighted / total >= 0.55


def _likely_section_heading(text: str) -> bool:
    value = text.strip()
    if not value or value.endswith((":", ".", ",", ";")) or "," in value:
        return False
    words = re.findall(r"[A-Za-z][A-Za-z0-9-]*", value)
    if not 1 <= len(words) <= 12:
        return False
    content_words = [
        word for word in words if word.casefold() not in {"a", "an", "and", "for", "of", "the", "to"}
    ]
    titled = sum(word[:1].isupper() for word in content_words)
    return bool(content_words) and titled / len(content_words) >= 0.5


def _make_block(
    source_sha256: str,
    run_id: str,
    ordinal: int,
    kind: str,
    status: str,
    disposition: str,
    lines: list[LayoutLine],
    page: PdfPageObservation,
    *,
    issues: list[str],
    text: str | None = None,
) -> PdfBlock:
    object_refs = [object_ref for line in lines for object_ref in line.object_refs]
    bbox = _union_bbox([line.bbox for line in lines])
    text = _join_paragraph_lines(lines, set())[0] if text is None else text
    raw_text = "\n".join(line.raw_text for line in lines)
    transformations = []
    if raw_text != text:
        transformations.append(
            {
                "kind": "join_line",
                "input_refs": object_refs,
                "before": raw_text,
                "after": text,
                "rule": "pdf-layout-v1",
            }
        )
    return PdfBlock(
        1,
        make_block_id(source_sha256, run_id, object_refs),
        source_sha256,
        run_id,
        ordinal,
        kind,
        status,  # type: ignore[arg-type]
        disposition,  # type: ignore[arg-type]
        {"overall": 1.0 if status == "ok" else None, "text": 1.0, "kind": None, "order": 1.0},
        [
            {
                "page": page.page,
                "bbox": bbox,
                "page_width": page.width,
                "page_height": page.height,
                "media_box": page.media_box,
                "crop_box": page.crop_box,
                "rotation": page.rotation,
                "coord_space": "pdf_points",
                "origin": "top_left",
            }
        ],
        object_refs,
        raw_text,
        text,
        None,
        None,
        [],
        None,
        None,
        transformations,
        issues,
        f"pages/{page.page}/layout/{ordinal}",
    )


def _join_paragraph_lines(
    lines: list[LayoutLine], document_tokens: set[str]
) -> tuple[str, bool]:
    output = ""
    ambiguous = False
    for line in lines:
        if not output:
            output = line.text
        elif output.endswith("-") and line.text[:1].islower():
            head = re.match(r"[A-Za-z]+", line.text)
            tail = re.search(r"[A-Za-z]+$", output[:-1])
            attached = bool(re.search(r"[A-Za-z]-$", output))
            if (
                attached
                and head
                and tail
                and (tail.group(0) + head.group(0)).casefold() in document_tokens
            ):
                # Soft syllable break corroborated elsewhere in the document: join.
                output = output[:-1] + line.text
            else:
                # Genuine compound or uncorroborated break: keep the hyphen.
                output += line.text
                ambiguous = True
        else:
            output += " " + line.text
    return re.sub(r"\s+", " ", output).strip(), ambiguous


def _heading_level(text: str) -> int | None:
    compact = re.sub(r"\s+", "", text).casefold()
    if compact in {"abstract", "references"}:
        return 2
    match = re.match(r"^(\d+(?:\.\d+)*)\.?\s+\S", text)
    if not match:
        return None
    return min(2 + match.group(1).count("."), 6)


def _is_heading_line(line: LayoutLine) -> bool:
    compact = re.sub(r"\s+", "", line.text).casefold()
    if compact in KNOWN_HEADINGS:
        return True
    if "bold" not in line.fontname.casefold():
        return False
    return _heading_level(line.text) is not None


def _heading_candidate_line(line: LayoutLine) -> bool:
    if _is_heading_line(line):
        return True
    text = line.text.strip()
    return (
        "bold" in line.fontname.casefold()
        and len(text) <= 120
        and len(text.split()) <= 16
        and not text.endswith((".", ",", ";"))
    )


def _looks_like_page_number(text: str) -> bool:
    return bool(re.fullmatch(r"(?:\d+|[ivxlcdm]+)", text.strip(), re.IGNORECASE))


def _document_tokens(lines_by_page: dict[int, list[LayoutLine]]) -> set[str]:
    tokens: set[str] = set()
    for lines in lines_by_page.values():
        for line in lines:
            for token in re.findall(r"[A-Za-z]{2,}", line.text):
                tokens.add(token.casefold())
    return tokens


def _is_decorative_visual(item: RawPdfObject, policy: dict[str, Any]) -> bool:
    """Annotations and hairline rules are artifacts, not recoverable content."""
    if item.kind == "annotation":
        return True
    if item.kind == "image_occurrence":
        return False
    x0, y0, x1, y1 = item.bbox
    return min(x1 - x0, y1 - y0) <= float(policy["rule_thickness_pt"])


def _valid_unicode(value: str) -> bool:
    if not value or value == "\ufffd" or re.fullmatch(r"\(cid:\d+\)", value):
        return False
    point = ord(value[0])
    return not (0xD800 <= point <= 0xDFFF or 0xE000 <= point <= 0xF8FF or point < 32)


def _figure_visuals(
    page_objects: list[RawPdfObject], caption_bbox: list[float], policy: dict[str, Any]
) -> tuple[list[RawPdfObject], list[float] | None]:
    """Return content-bearing visuals (images, curves) beside a figure caption."""
    gap = float(policy["table_region_gap_pt"])
    visuals = [
        item
        for item in page_objects
        if item.kind in {"image_occurrence", "curve"}
        and _adjacent_bbox(item.bbox, caption_bbox, gap)
    ]
    if not visuals:
        return [], None
    return visuals, _union_bbox([item.bbox for item in visuals])


def _adjacent_bbox(bbox: list[float], caption: list[float], gap: float) -> bool:
    x0, y0, x1, y1 = bbox
    cx0, cy0, cx1, cy1 = caption
    return min(abs(y0 - cy1), abs(y1 - cy0)) <= gap and min(abs(x0 - cx1), abs(x1 - cx0)) <= gap + (cx1 - cx0)


def _caption_label(text: str, kind: str) -> str:
    pattern = r"(?:Figure|Fig\.?)\s+\d+" if kind == "figure" else r"Table\s+\d+"
    match = re.match(pattern, text, re.IGNORECASE)
    return match.group(0) if match else text.strip()


def _build_elements(
    page_blocks: list[PdfBlock],
    page: PdfPageObservation,
    page_objects: list[RawPdfObject],
    source_sha256: str,
    run_id: str,
    policy: dict[str, Any],
    ordinal_start: int,
    visual_clusters: list[pdf_visual.VisualCluster],
    claimed_visual_refs: set[str],
) -> tuple[
    list[PdfBlock],
    dict[int, PdfBlock],
    dict[str, tuple[str, str, str | None]],
    dict[int, tuple[str, PdfBlock]],
]:
    """Resolve captions into table/figure blocks.

    Returns ``(new_blocks, updated_captions, claims, placements)`` where
    ``claims`` maps an object_ref to ``(disposition, block_id, reason_code)``.
    ``updated_captions`` maps a caption's *ordinal* to a status-``ok`` copy (the
    caption text itself is faithful and translatable).
    """
    elements: list[PdfBlock] = []
    updated: dict[int, PdfBlock] = {}
    claims: dict[str, tuple[str, str, str | None]] = {}
    placements: dict[int, tuple[str, PdfBlock]] = {}
    ordinal = ordinal_start
    for block in page_blocks:
        if block.kind == "table_caption":
            caption_bbox = block.provenance[0]["bbox"]
            table_objects = [
                item for item in page_objects if item.object_ref not in claimed_visual_refs
            ]
            resolved = _resolve_table_structure(table_objects, caption_bbox, policy)
            grid = resolved[0] if resolved else None
            rules = resolved[1]
            style = resolved[2]
            if grid is not None:
                in_grid_chars = _chars_in_region(table_objects, grid, policy)
                assignment = pdf_table.assign_chars_to_cells(in_grid_chars, grid, policy)
            else:
                assignment = None
            if grid is not None and assignment is not None and pdf_table.verified(grid, assignment, policy):
                refs = [item.object_ref for item in rules] + [
                    item.object_ref for item in in_grid_chars
                ]
                payload = pdf_table.build_payload(grid, assignment, style=style)
                rule_bboxes = [item.bbox for item in rules] or [caption_bbox]
                line = _element_line(page, _union_bbox(rule_bboxes), "Table", refs)
                element = replace(
                    _make_block(
                        source_sha256, run_id, ordinal, "table", "ok", "render", [line], page,
                        issues=[], text="Table",
                    ),
                    table=payload,
                )
                # char accounting: in-grid chars move to the table block
                for item in in_grid_chars:
                    claims[item.object_ref] = ("rendered", element.block_id, None)
                for item in rules:
                    claims[item.object_ref] = ("rendered", element.block_id, None)
                elements.append(element)
                placements[block.ordinal] = ("after", element)
                ordinal += 1
                updated[block.ordinal] = replace(block, status="ok", issues=[], disposition="render")
            else:
                # Honest image fallback. When structure recovery found candidate rules
                # (boxed or light) we keep them as an unresolved visual placeholder;
                # a truly unruled table has no evidence to claim, so the unresolved
                # caption itself carries the incompleteness.
                if rules:
                    # The placeholder region must describe the whole evidence area,
                    # not just the detected rules: audit work orders and crops carve
                    # from this bbox, so content glyphs near the rules belong in it.
                    rules_region = _union_bbox([item.bbox for item in rules])
                    # Region = the rules plus every glyph vertically within a
                    # hair's breadth of them: interior rows and straddling edge
                    # glyphs belong to the table, distant prose does not (the
                    # audit gate would otherwise require grids to cover it).
                    row_margin = 10.0
                    top, bottom = rules_region[1], rules_region[3]
                    nearby_chars = [
                        item
                        for item in page_objects
                        if item.kind == "char"
                        and item.object_ref not in block.source_object_refs
                        and top - row_margin
                        <= (item.bbox[1] + item.bbox[3]) / 2
                        <= bottom + row_margin
                    ]
                    region_bbox = _union_bbox(
                        [list(item.bbox) for item in rules]
                        + [list(item.bbox) for item in nearby_chars]
                    )
                    line = _element_line(
                        page,
                        region_bbox,
                        "Unresolved table region.",
                        [item.object_ref for item in rules],
                    )
                    element = _make_block(
                        source_sha256, run_id, ordinal, "table", "unresolved",
                        "unresolved_placeholder", [line], page,
                        issues=["PDF_TABLE_UNRESOLVED"], text="Unresolved table region.",
                    )
                    for item in rules:
                        claims[item.object_ref] = (
                            "unresolved", element.block_id, "PDF_TABLE_UNRESOLVED"
                        )
                    elements.append(element)
                    placements[block.ordinal] = ("after", element)
                    ordinal += 1
                    updated[block.ordinal] = replace(
                        block, status="ok", issues=[], disposition="render"
                    )
        elif block.kind == "figure_caption":
            caption_bbox = block.provenance[0]["bbox"]
            # Rule objects claimed by an unresolved-table placeholder follow
            # reading-order priority and stay with the table; they must not make
            # the whole neighboring visual cluster unavailable to this figure.
            staked_rule_refs = {
                object_ref
                for object_ref, (disposition, _, _) in claims.items()
                if disposition == "unresolved"
            }
            selected = pdf_visual.figure_clusters(
                visual_clusters,
                caption_bbox,
                set(claims) - staked_rule_refs,
                max_gap=float(policy["table_region_gap_pt"]),
            )
            visuals = [
                item
                for cluster in selected
                for item in cluster.objects
                if item.object_ref not in claims
            ]
            region = _union_bbox([list(item.bbox) for item in visuals]) if visuals else None
            label = _caption_label(block.text or "", "figure")
            if visuals and region:
                # A concrete visual (embedded image or vector cluster) is capturable as an
                # asset with a bbox; only a caption with no visual stays unresolved.
                figure_chars = [
                    item
                    for item in chars_in_bbox(page_objects, region, policy)
                    if item.object_ref not in block.source_object_refs
                ]
                refs = [item.object_ref for item in visuals + figure_chars]
                line = _element_line(page, region, label, refs)
                element = _make_block(
                    source_sha256, run_id, ordinal, "figure", "ok", "render",
                    [line], page, issues=[], text=label,
                )
                for item in visuals + figure_chars:
                    claims[item.object_ref] = ("rendered", element.block_id, None)
                elements.append(element)
                placements[block.ordinal] = ("before", element)
                ordinal += 1
                updated[block.ordinal] = replace(block, status="ok", issues=[], disposition="render")
    return elements, updated, claims, placements


def _verified_figure_chars(
    page: PdfPageObservation,
    lines: list[LayoutLine],
    document_tokens: set[str],
    artifact_keys: set[str],
    visual_clusters: list[pdf_visual.VisualCluster],
    policy: dict[str, Any],
    stake_boxes: list[list[float]],
) -> tuple[set[str], set[str]]:
    """Return captured chart chars and visual refs from one caption decision.

    ``stake_boxes`` are rule-evidence regions staked out for table captions:
    claim priority follows reading order, so figure clusters may not swallow a
    nearby table's rule evidence even when that table will not promote and
    falls back to an honest crop placeholder.
    """
    artifact_lines = [
        line for line in lines if _artifact_key(page, line, policy) in artifact_keys
    ]
    content_lines = [line for line in lines if line not in artifact_lines]
    ordered, _ = _reading_order(page, content_lines, policy)
    paragraphs = _paragraphs(ordered)
    consumed: set[str] = set()
    claimed_visuals: set[str] = set()
    tolerance = float(policy["table_cell_overlap_tolerance_pt"])
    for paragraph in paragraphs:
        text, ambiguous_hyphen = _join_paragraph_lines(paragraph, document_tokens)
        kind, _, _ = _classify_block(text, False, paragraph, ambiguous_hyphen)
        if kind != "figure_caption":
            continue
        caption_bbox = _union_bbox([line.bbox for line in paragraph])
        selected = pdf_visual.figure_clusters(
            visual_clusters,
            caption_bbox,
            claimed_visuals,
            max_gap=float(policy["table_region_gap_pt"]),
        )
        if not selected:
            continue
        kept = [
            item
            for cluster in selected
            for item in cluster.objects
            if not any(
                _boxes_overlap(list(item.bbox), box, tolerance) for box in stake_boxes
            )
        ]
        if not kept:
            continue
        claimed_visuals.update(item.object_ref for item in kept)
        region = _union_bbox([list(item.bbox) for item in kept])
        consumed.update(
            item.object_ref
            for item in chars_in_bbox(page.objects, region, policy)
            if item.object_ref not in {
                object_ref for line in paragraph for object_ref in line.object_refs
            }
        )
    return consumed, claimed_visuals


def _boxes_overlap(a: list[float], b: list[float], tolerance: float) -> bool:
    return (
        a[0] <= b[2] + tolerance
        and b[0] <= a[2] + tolerance
        and a[1] <= b[3] + tolerance
        and b[1] <= a[3] + tolerance
    )


def _table_stake_boxes(
    page: PdfPageObservation,
    lines: list[LayoutLine],
    document_tokens: set[str],
    artifact_keys: set[str],
    policy: dict[str, Any],
) -> list[list[float]]:
    """Union boxes of rule evidence beside each table caption on the page."""
    artifact_lines = [line for line in lines if _artifact_key(page, line, policy) in artifact_keys]
    content_lines = [line for line in lines if line not in artifact_lines]
    ordered, _ = _reading_order(page, content_lines, policy)
    boxes: list[list[float]] = []
    for paragraph in _paragraphs(ordered):
        text, ambiguous_hyphen = _join_paragraph_lines(paragraph, document_tokens)
        kind, _, _ = _classify_block(text, False, paragraph, ambiguous_hyphen)
        if kind != "table_caption":
            continue
        caption_bbox = _union_bbox([line.bbox for line in paragraph])
        rules = pdf_table._table_rules(page.objects, caption_bbox, policy)
        if rules:
            boxes.append(_union_bbox([list(item.bbox) for item in rules]))
    return boxes


def _resolve_table_structure(
    page_objects: list[RawPdfObject],
    caption_bbox: list[float],
    policy: dict[str, Any],
) -> tuple[pdf_table.TableGrid | None, list[Any], str]:
    """Decide once whether a captioned table promotes, in either supported style.

    Returns ``(grid, rules, style)``; ``grid`` is ``None`` when nothing can be
    promoted honestly, in which case ``rules`` still carries the boxed-path
    heuristic rules for the fallback placeholder claim.
    """
    rules = pdf_table._table_rules(page_objects, caption_bbox, policy)
    grid = pdf_table.reconstruct_grid(page_objects, caption_bbox, policy)
    if grid is not None:
        return grid, rules, "boxed"
    plan = pdf_table.plan_light_grid(page_objects, caption_bbox, policy)
    if plan is not None:
        return plan.grid, list(plan.rules), "booktabs"
    return None, rules, ""


def _verified_table_chars(
    page: PdfPageObservation,
    lines: list[LayoutLine],
    document_tokens: set[str],
    artifact_keys: set[str],
    policy: dict[str, Any],
) -> set[str]:
    """Return char refs that belong to a verified (structured, accounted) table.

    These characters are removed from the text layer so they are not rendered
    twice — once as body text and once as the structured table. Detection is a
    cheap pre-pass: it needs the captions that only the text layer can surface.
    """
    artifact_lines = [line for line in lines if _artifact_key(page, line, policy) in artifact_keys]
    content_lines = [line for line in lines if line not in artifact_lines]
    ordered, _ = _reading_order(page, content_lines, policy)
    paragraphs = _paragraphs(ordered)
    consumed: set[str] = set()
    for paragraph in paragraphs:
        text, ambiguous_hyphen = _join_paragraph_lines(paragraph, document_tokens)
        kind, _, _ = _classify_block(text, False, paragraph, ambiguous_hyphen)
        if kind != "table_caption":
            continue
        caption_bbox = _union_bbox([line.bbox for line in paragraph])
        resolved = _resolve_table_structure(page.objects, caption_bbox, policy)
        grid = resolved[0] if resolved else None
        if grid is None:
            continue
        in_grid = _chars_in_region(page.objects, grid, policy)
        assignment = pdf_table.assign_chars_to_cells(in_grid, grid, policy)
        if pdf_table.verified(grid, assignment, policy):
            consumed.update(char.object_ref for char in in_grid)
    return consumed


def _chars_in_region(
    page_objects: list[RawPdfObject], grid: pdf_table.TableGrid, policy: dict[str, Any]
) -> list[RawPdfObject]:
    tolerance = float(policy["table_cell_overlap_tolerance_pt"])
    x0, y0 = grid.x_bounds[0], grid.y_bounds[0]
    x1, y1 = grid.x_bounds[-1], grid.y_bounds[-1]
    return [
        char
        for char in page_objects
        if char.kind == "char"
        and char.payload
        and x0 - tolerance <= char.bbox[0]
        and char.bbox[2] <= x1 + tolerance
        and y0 - tolerance <= char.bbox[1]
        and char.bbox[3] <= y1 + tolerance
    ]


def chars_in_bbox(
    page_objects: list[RawPdfObject], bbox: list[float], policy: dict[str, Any]
) -> list[RawPdfObject]:
    tolerance = float(policy["table_cell_overlap_tolerance_pt"])
    x0, y0, x1, y1 = bbox
    return [
        char
        for char in page_objects
        if char.kind == "char"
        and char.payload
        and x0 - tolerance <= (char.bbox[0] + char.bbox[2]) / 2 <= x1 + tolerance
        and y0 - tolerance <= (char.bbox[1] + char.bbox[3]) / 2 <= y1 + tolerance
    ]


def _element_line(page: PdfPageObservation, bbox: list[float], text: str, refs: list[str]) -> LayoutLine:
    return LayoutLine(page.page, bbox, text, text, refs, 0.0, "", page.width)


def _equation_visual_overlap(
    candidate: pdf_equation.EquationCandidate,
    page_objects: list[RawPdfObject],
    policy: dict[str, Any],
) -> bool:
    x0, y0, x1, y1 = candidate.bbox
    tolerance = float(policy["table_cell_overlap_tolerance_pt"])
    return any(
        item.kind in {"image_occurrence", "curve"}
        and item.bbox[2] >= x0 - tolerance
        and item.bbox[0] <= x1 + tolerance
        and item.bbox[3] >= y0 - tolerance
        and item.bbox[1] <= y1 + tolerance
        for item in page_objects
    )


def _union_bbox(boxes: list[list[float]]) -> list[float]:
    return [
        round(min(box[0] for box in boxes), 4),
        round(min(box[1] for box in boxes), 4),
        round(max(box[2] for box in boxes), 4),
        round(max(box[3] for box in boxes), 4),
    ]


def _mark_reference_blocks(blocks: list[PdfBlock]) -> list[PdfBlock]:
    in_references = False
    output: list[PdfBlock] = []
    for block in blocks:
        compact = re.sub(r"\s+", "", block.text or "").casefold()
        if block.kind == "section_heading" and compact == "references":
            in_references = True
        elif in_references and block.kind == "paragraph" and block.status == "ok":
            block = replace(block, kind="reference")
        output.append(block)
    return output


def _mark_front_matter(blocks: list[PdfBlock]) -> list[PdfBlock]:
    first_section = next(
        (index for index, block in enumerate(blocks) if block.kind == "section_heading"),
        None,
    )
    if first_section is None:
        return blocks
    return [
        replace(block, kind="metadata")
        if index < first_section and block.kind == "paragraph" and block.status == "ok"
        else block
        for index, block in enumerate(blocks)
    ]


def _mark_cross_page_ambiguities(
    blocks: list[PdfBlock],
) -> tuple[list[PdfBlock], set[str]]:
    by_page: dict[int, list[PdfBlock]] = defaultdict(list)
    for block in blocks:
        if block.disposition == "render":
            by_page[block.provenance[0]["page"]].append(block)
    ambiguous: set[str] = set()
    for page in sorted(by_page):
        if page + 1 not in by_page:
            continue
        left = next(
            (item for item in reversed(by_page[page]) if item.kind in {"paragraph", "reference"}),
            None,
        )
        right = next(
            (item for item in by_page[page + 1] if item.kind in {"paragraph", "reference"}),
            None,
        )
        if left is None or right is None:
            continue
        left_text, right_text = (left.text or "").rstrip(), (right.text or "").lstrip()
        if left_text and right_text[:1].islower() and not left_text.endswith((".", "?", "!")):
            ambiguous.update({left.block_id, right.block_id})
    output = [
        replace(
            block,
            status="unresolved",
            disposition="unresolved_placeholder",
            issues=sorted({*block.issues, "PDF_CROSS_PAGE_CONTINUATION_UNRESOLVED"}),
        )
        if block.block_id in ambiguous
        else block
        for block in blocks
    ]
    return output, ambiguous


def _merge_cross_page_continuations(
    blocks: list[PdfBlock],
) -> tuple[list[PdfBlock], dict[str, str]]:
    output = list(blocks)
    replacements: dict[str, str] = {}
    while True:
        merged = False
        end_by_page: dict[int, list[tuple[int, PdfBlock]]] = defaultdict(list)
        start_by_page: dict[int, list[tuple[int, PdfBlock]]] = defaultdict(list)
        for index, block in enumerate(output):
            if block.kind != "paragraph" or block.status != "ok" or block.issues:
                continue
            start_by_page[block.provenance[0]["page"]].append((index, block))
            end_by_page[block.provenance[-1]["page"]].append((index, block))
        for page in sorted(end_by_page):
            if page + 1 not in start_by_page:
                continue
            left_index, left = max(
                end_by_page[page], key=lambda item: item[1].provenance[-1]["bbox"][3]
            )
            right_index, right = min(
                start_by_page[page + 1], key=lambda item: item[1].provenance[0]["bbox"][1]
            )
            if not _certain_cross_page_continuation(left, right):
                continue
            refs = left.source_object_refs + right.source_object_refs
            merged_id = make_block_id(left.source_sha256, left.run_id, refs)
            merged_block = replace(
                left,
                block_id=merged_id,
                provenance=left.provenance + right.provenance,
                source_object_refs=refs,
                raw_text=(left.raw_text or "") + "\n" + (right.raw_text or ""),
                text=(left.text or "").rstrip() + " " + (right.text or "").lstrip(),
                transformations=left.transformations
                + right.transformations
                + [
                    {
                        "kind": "join_cross_page",
                        "input_refs": refs,
                        "before": (left.text or "") + "\n" + (right.text or ""),
                        "after": (left.text or "").rstrip()
                        + " "
                        + (right.text or "").lstrip(),
                        "rule": "pdf-layout-v1",
                    }
                ],
            )
            replacements[left.block_id] = merged_id
            replacements[right.block_id] = merged_id
            output[left_index] = merged_block
            output.pop(right_index)
            merged = True
            break
        if not merged:
            return output, replacements


def _certain_cross_page_continuation(left: PdfBlock, right: PdfBlock) -> bool:
    left_text = (left.text or "").rstrip()
    right_text = (right.text or "").lstrip()
    left_provenance = left.provenance[-1]
    right_provenance = right.provenance[0]
    return bool(
        left_text
        and right_text[:1].islower()
        and not left_text.endswith((".", "?", "!", ":", ";", "-"))
        and left_provenance["bbox"][3] >= left_provenance["page_height"] * 0.88
        and right_provenance["bbox"][1] <= right_provenance["page_height"] * 0.18
        and abs(left_provenance["bbox"][0] - right_provenance["bbox"][0]) <= 12.0
    )


def _final_block_id(block_id: str | None, replacements: dict[str, str]) -> str | None:
    while block_id in replacements and replacements[block_id] != block_id:
        block_id = replacements[block_id]
    return block_id
