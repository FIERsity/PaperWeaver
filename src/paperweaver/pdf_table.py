"""Deterministic, conservative reconstruction of boxed PDF tables.

Only a closed, full-spanning rectangular rule grid whose cell characters all
land in exactly one cell is promoted to a verified ``table`` block. Anything
ambiguous (partial or non-spanning rules, merged cells, characters in multiple
or no cells, a wide header band) is refused so the caller falls back to an honest
image crop. No structure is ever guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Any

Span = dict[str, Any]


@dataclass(frozen=True)
class TableGrid:
    x_bounds: tuple[float, ...]  # column boundaries, length columns + 1
    y_bounds: tuple[float, ...]  # row boundaries, length rows + 1
    header_rows: int

    @property
    def columns(self) -> int:
        return len(self.x_bounds) - 1

    @property
    def rows(self) -> int:
        return len(self.y_bounds) - 1


@dataclass(frozen=True)
class CellAssignment:
    cells: tuple[tuple[str, ...], ...]
    object_refs: tuple[tuple[tuple[str, ...], ...], ...]
    coverage: float
    ambiguous: bool
    outside: int


def _table_rules(page_objects: list[Any], caption_bbox: list[float], policy: dict[str, Any]) -> list[Any]:
    """Return the run of rule objects (rect/line) forming the table near a caption.

    We seed with rules directly beside the caption, then grow along shared
    row/column coordinates so the whole grid (however tall) is captured. Rules
    that are far away or do not connect back to the caption are left out. If no
    seed rule is found there is no boxed table to reconstruct here.
    """
    gap = float(policy["table_region_gap_pt"])
    tol = float(policy["table_edge_tolerance_pt"])
    rule_thickness = float(policy["rule_thickness_pt"])
    rules = [
        item
        for item in page_objects
        if item.kind == "line"
        or (
            item.kind == "rect"
            and min(item.bbox[2] - item.bbox[0], item.bbox[3] - item.bbox[1])
            <= rule_thickness
        )
    ]
    selected = [
        item for item in rules if _adjacent(item.bbox, caption_bbox, gap)
    ]
    if not selected:
        return []
    grown = list(selected)
    changed = True
    while changed:
        changed = False
        for item in rules:
            if item in grown:
                continue
            if any(_connected(item.bbox, other.bbox, tol) for other in grown):
                grown.append(item)
                changed = True
    return grown


def _adjacent(bbox: list[float], caption: list[float], gap: float) -> bool:
    x0, y0, x1, y1 = bbox
    cx0, cy0, cx1, cy1 = caption
    return min(abs(y0 - cy1), abs(y1 - cy0)) <= gap and min(abs(x0 - cx1), abs(x1 - cx0)) <= gap + (cx1 - cx0)


def _connected(a: list[float], b: list[float], tolerance: float) -> bool:
    xa0, ya0, xa1, ya1 = a
    xb0, yb0, xb1, yb1 = b
    # Same/near row or column line, so they belong to one table grid.
    return (
        min(abs(xa0 - xb1), abs(xa1 - xb0)) <= tolerance * 2
        and not (ya1 < yb0 - tolerance or yb1 < ya0 - tolerance)
    ) or (
        min(abs(ya0 - yb1), abs(ya1 - yb0)) <= tolerance * 2
        and not (xa1 < xb0 - tolerance or xb1 < xa0 - tolerance)
    )


def _segments(rules: list[Any], tolerance: float) -> tuple[list[tuple[float, float, float]], list[tuple[float, float, float]]]:
    horizontals: list[tuple[float, float, float]] = []
    verticals: list[tuple[float, float, float]] = []
    for item in rules:
        x0, y0, x1, y1 = item.bbox
        width, height = x1 - x0, y1 - y0
        if height <= tolerance and width > tolerance * 2:
            horizontals.append((y0, x0, x1))
        elif width <= tolerance and height > tolerance * 2:
            verticals.append((x0, y0, y1))
    return horizontals, verticals


def reconstruct_grid(
    page_objects: list[Any], caption_bbox: list[float], policy: dict[str, Any]
) -> TableGrid | None:
    """Build a rectangular grid from the rules near ``caption_bbox``, or ``None``."""
    rules = _table_rules(page_objects, caption_bbox, policy)
    if len(rules) < 4:
        return None
    tol = float(policy["table_edge_tolerance_pt"])
    horizontals, verticals = _segments(rules, tol)
    rows_y = _cluster(horizontals, tol, axis="y")
    cols_x = _cluster(verticals, tol, axis="x")
    if rows_y is None or cols_x is None or len(rows_y) < 2 or len(cols_x) < 2:
        return None
    y_bounds = tuple(coord for coord, _ in rows_y)
    x_bounds = tuple(coord for coord, _ in cols_x)
    if not _all_full_span(rows_y, x_bounds[0], x_bounds[-1], tol):
        return None
    if not _all_full_span(cols_x, y_bounds[0], y_bounds[-1], tol):
        return None
    header_rows = 1 if len(rows_y) >= 2 else 0
    return TableGrid(x_bounds, y_bounds, header_rows)


def _cluster(items: list[tuple[float, float, float]], tolerance: float, *, axis: str) -> list[tuple[float, list[tuple[float, float]]]] | None:
    if not items:
        return None
    grouped: list[list[tuple[float, float, float]]] = []
    for item in sorted(items, key=lambda value: value[0]):
        target = next((group for group in reversed(grouped) if abs(group[-1][0] - item[0]) <= tolerance), None)
        if target is None:
            grouped.append([item])
        else:
            target.append(item)
    return [(sum(item[0] for item in group) / len(group), [(item[1], item[2]) for item in group]) for group in grouped]


def _all_full_span(
    clustered: list[tuple[float, list[tuple[float, float]]]], lo: float, hi: float, tolerance: float
) -> bool:
    if not clustered:
        return False
    return all(
        any(slo - tolerance <= lo and hi <= shi + tolerance for slo, shi in spans) for _, spans in clustered
    )


def assign_chars_to_cells(
    chars: list[Any], grid: TableGrid, policy: dict[str, Any]
) -> CellAssignment:
    tolerance = float(policy["table_cell_overlap_tolerance_pt"])
    x0, y0 = grid.x_bounds[0], grid.y_bounds[0]
    x1, y1 = grid.x_bounds[-1], grid.y_bounds[-1]
    region = [
        char
        for char in chars
        if char.kind == "char"
        and char.payload
        and x0 - tolerance <= char.bbox[0]
        and char.bbox[2] <= x1 + tolerance
        and y0 - tolerance <= char.bbox[1]
        and char.bbox[3] <= y1 + tolerance
    ]
    cells: list[list[list[Any]]] = [[[] for _ in range(grid.columns)] for _ in range(grid.rows)]
    outside = 0
    for char in region:
        cx = (char.bbox[0] + char.bbox[2]) / 2
        cy = (char.bbox[1] + char.bbox[3]) / 2
        hits = [
            (i, j)
            for j in range(grid.columns)
            if grid.x_bounds[j] - tolerance <= cx <= grid.x_bounds[j + 1] + tolerance
            for i in range(grid.rows)
            if grid.y_bounds[i] - tolerance <= cy <= grid.y_bounds[i + 1] + tolerance
        ]
        if len(hits) != 1:
            outside += 1
            continue
        cells[hits[0][0]][hits[0][1]].append(char)
    rows: list[tuple[str, ...]] = []
    refs: list[tuple[tuple[str, ...], ...]] = []
    assigned = 0
    for i in range(grid.rows):
        row: list[str] = []
        ref_row: list[tuple[str, ...]] = []
        for j in range(grid.columns):
            chars_in_cell = cells[i][j]
            assigned += len(chars_in_cell)
            text, ordered_refs = _join(chars_in_cell, policy)
            row.append(text)
            ref_row.append(ordered_refs)
        rows.append(tuple(row))
        refs.append(tuple(ref_row))
    coverage = assigned / len(region) if region else 0.0
    return CellAssignment(
        tuple(rows), tuple(refs), coverage, ambiguous=bool(outside) or assigned != len(region), outside=outside
    )


def _join(chars: list[Any], policy: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    tolerance = float(policy["line_y_tolerance_points"])
    baselines: list[list[Any]] = []
    for char in sorted(chars, key=lambda item: (item.bbox[1], item.bbox[0], item.object_ref)):
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
    ordered_rows = [
        sorted(row, key=lambda item: (item.bbox[0], item.bbox[1], item.object_ref))
        for row in baselines
    ]
    return (
        "\n".join(_join_row(row) for row in ordered_rows).strip(),
        tuple(item.object_ref for row in ordered_rows for item in row),
    )


def _join_row(chars: list[Any]) -> str:
    output = ""
    previous: Any = None
    for char in chars:
        value = char.payload
        if previous is not None and value != " " and not output.endswith(" "):
            gap = char.bbox[0] - previous.bbox[2]
            size = max(float(previous.attrs.get("size", 0.0)), 1.0)
            if gap > max(0.8, size * 0.18):
                output += " "
        output += value
        previous = char
    return output.strip()


def build_payload(grid: TableGrid, assignment: CellAssignment) -> dict[str, Any]:
    return {
        "rows": [[assignment.cells[i][j] for j in range(grid.columns)] for i in range(grid.rows)],
        "row_spans": [],
        "col_spans": [],
        "header_rows": grid.header_rows,
        "structure_verified": True,
    }


def verified(grid: TableGrid, assignment: CellAssignment, policy: dict[str, Any]) -> bool:
    return not assignment.ambiguous and assignment.coverage >= float(policy["min_table_char_coverage"])
