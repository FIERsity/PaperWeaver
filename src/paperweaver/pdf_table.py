"""Deterministic, conservative reconstruction of boxed and rule-light PDF tables.

Two promoted structures exist, both requiring every cell character to land in
exactly one cell:

* **Boxed grids** — a closed, full-spanning rectangular rule grid reconstructed
  from rule geometry alone. Rule carriers may be ``line``/``rect`` strokes or
  axis-aligned ``curve`` hairlines (some generators outline tables as Bézier
  bars with bboxes just as thin).
* **Rule-light (booktabs-style) tables** — a band of near-full-width horizontal
  rules below a caption with strictly no vertical rules inside the band. Column
  boundaries are inferred only from character geometry (full-height empty
  stripes), never from semantics.

Anything ambiguous (partial vertical rules, merged cells, characters in multiple
or no cells, empty inferred columns) is refused so the caller falls back to an
honest image crop. No structure is ever guessed. Records inside a data band that
has no interior row separators are intentionally not split: their baselines are
joined with newlines inside each cell, exactly like multi-line boxed cells.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
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


@dataclass(frozen=True)
class LightGridPlan:
    """A promoted rule-light table: its grid plus the rules that evidence it."""

    grid: TableGrid
    rules: tuple[Any, ...]


def _rule_like(item: Any, rule_thickness: float) -> bool:
    """Whether the object can carry a table rule: straight strokes or thin bars."""
    if item.kind == "line":
        return True
    if item.kind not in {"rect", "curve"}:
        return False
    return (
        min(item.bbox[2] - item.bbox[0], item.bbox[3] - item.bbox[1])
        <= rule_thickness
    )


def _table_rules(
    page_objects: list[Any], caption_bbox: list[float], policy: dict[str, Any]
) -> list[Any]:
    """Return the run of rule objects forming the table beside/below a caption.

    Seeds sit strictly under the caption's bottom edge (a caption-underlining
    page decoration above it must not hijack the seed), then grow along shared
    row/column coordinates so the whole grid (however tall) is captured without
    reaching above the top rule or into unrelated components elsewhere.
    """
    gap = float(policy["table_region_gap_pt"])
    tol = float(policy["table_edge_tolerance_pt"])
    rule_thickness = float(policy["rule_thickness_pt"])
    rules = [
        item
        for item in page_objects
        if _rule_like(item, rule_thickness)
    ]
    selected = [
        item
        for item in rules
        if item.bbox[1] >= caption_bbox[3] - tol * 2
        and _adjacent(item.bbox, caption_bbox, gap)
    ]
    if not selected:
        return []
    floor = min(item.bbox[1] for item in selected) - tol
    ceiling = max(item.bbox[1] for item in selected) + float(
        policy["table_max_rule_span_pt"]
    )
    grown = list(selected)
    changed = True
    while changed:
        changed = False
        for item in rules:
            if (
                item in grown
                or item.bbox[1] < floor
                or item.bbox[3] > ceiling
            ):
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
    lattice = _lattice_core(horizontals, verticals, tol)
    if lattice is None:
        return None
    x_bounds, y_bounds = lattice
    return TableGrid(x_bounds, y_bounds, header_rows=1)


def _group_axis_lines(
    lines: list[tuple[float, float, float]], tolerance: float
) -> list[tuple[float, list[tuple[float, float]]]]:
    """Cluster axis-aligned segments along their dominant axis (ascending)."""
    grouped: list[tuple[float, list[tuple[float, float]]]] = []
    for primary, lo, hi in sorted(lines, key=lambda value: value[0]):
        if grouped and abs(grouped[-1][0] - primary) <= tolerance:
            grouped[-1][1].append((lo, hi))
            grouped[-1] = (
                (grouped[-1][0] * (len(grouped[-1][1]) - 1) + primary)
                / len(grouped[-1][1]),
                grouped[-1][1],
            )
        else:
            grouped.append((primary, [(lo, hi)]))
    return grouped


def _covers(lo: float, hi: float, target_lo: float, target_hi: float, tolerance: float) -> bool:
    return lo - tolerance <= target_lo and target_hi <= hi + tolerance


def _union_covers(
    spans: list[tuple[float, float]], lo: float, hi: float, tolerance: float
) -> bool:
    merged: list[list[float]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1] + tolerance:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return any(_covers(s0, s1, lo, hi, tolerance) for s0, s1 in merged)


def _lattice_core(
    horizontals: list[tuple[float, float, float]],
    verticals: list[tuple[float, float, float]],
    tolerance: float,
) -> tuple[tuple[float, ...], tuple[float, ...]] | None:
    """Distill the dominant rectangular lattice from raw rule segments.

    Each iteration keeps row lines whose union touches the full column extent
    and column lines touching the full row extent, then recomputes extents;
    isolated page furniture (bullet ticks, stray hairlines) drops out while
    genuinely connecting lines remain. Nothing structural is invented: lines
    that never connect are simply not load-bearing.
    """
    active_h = list(horizontals)
    active_v = list(verticals)
    result: tuple[tuple[float, ...], tuple[float, ...]] | None = None
    for _ in range(3):
        rows = _group_axis_lines(active_h, tolerance)
        cols = _group_axis_lines(active_v, tolerance)
        if len(rows) < 2 or len(cols) < 2:
            return result
        x_lo, x_hi = cols[0][0], cols[-1][0]
        y_lo, y_hi = rows[0][0], rows[-1][0]
        next_h = [
            (primary, lo, hi)
            for primary, spans in rows
            if _union_covers(spans, x_lo, x_hi, tolerance)
            for lo, hi in spans
        ]
        next_v = [
            (primary, lo, hi)
            for primary, spans in cols
            if _union_covers(spans, y_lo, y_hi, tolerance)
            for lo, hi in spans
        ]
        if len(next_h) < 4 or len(next_v) < 4:
            return result
        if (len(next_h), len(next_v)) == (len(active_h), len(active_v)):
            rows = _group_axis_lines(next_h, tolerance)
            cols = _group_axis_lines(next_v, tolerance)
            return (
                tuple(primary for primary, _ in cols),
                tuple(primary for primary, _ in rows),
            )
        active_h, active_v = next_h, next_v
        result = None
    return result


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
    for _, spans in clustered:
        merged: list[list[float]] = []
        for start, end in sorted(spans):
            if merged and start <= merged[-1][1] + tolerance:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        if not any(s0 - tolerance <= lo and hi <= s1 + tolerance for s0, s1 in merged):
            return False
    return True


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


def build_payload(
    grid: TableGrid, assignment: CellAssignment, *, style: str = "boxed"
) -> dict[str, Any]:
    return {
        "rows": [[assignment.cells[i][j] for j in range(grid.columns)] for i in range(grid.rows)],
        "row_spans": [],
        "col_spans": [],
        "header_rows": grid.header_rows,
        "structure_verified": True,
        "structure_style": style,
    }


def verified(grid: TableGrid, assignment: CellAssignment, policy: dict[str, Any]) -> bool:
    return not assignment.ambiguous and assignment.coverage >= float(policy["min_table_char_coverage"])


def plan_light_grid(
    page_objects: list[Any],
    caption_bbox: list[float],
    policy: dict[str, Any],
) -> LightGridPlan | None:
    """Promote a rule-light (booktabs-style) table below ``caption_bbox``.

    Requires a top rule of near-full width directly under the caption and a
    chain of row rules whose gaps never exceed ``table_light_max_row_gap_pt``
    (so rules belonging to a following table are never absorbed); strictly no
    vertical rules may cross the resulting band. Columns come only from
    character geometry: full-height empty stripes between glyph intervals.
    Returns ``None`` for every uncertain shape — refusal is the default.
    """
    tolerance = float(policy["table_edge_tolerance_pt"])
    rule_thickness = float(policy["rule_thickness_pt"])
    candidates = [
        item
        for item in page_objects
        if _rule_like(item, rule_thickness)
        and _thin_horizontal(item.bbox, tolerance)
        and item.bbox[1] >= caption_bbox[3] - tolerance
    ]
    if len(candidates) < 2:
        return None
    # Seed on the locally widest rule near the caption: page-global widths are
    # dominated by unrelated elements elsewhere on the page.
    window = float(policy["table_region_gap_pt"])
    near = [item for item in candidates if item.bbox[1] <= caption_bbox[3] + window]
    seed_width = max((item.bbox[2] - item.bbox[0] for item in near), default=0.0)
    if seed_width < float(policy["table_light_min_rule_width_pt"]):
        return None
    minimum_segment = seed_width * float(policy["table_light_min_row_rule_ratio"])
    qualifying = [item for item in candidates if item.bbox[2] - item.bbox[0] >= minimum_segment]
    if len(qualifying) < 2:
        return None
    clustered = _cluster(
        [(item.bbox[1], item.bbox[0], item.bbox[2]) for item in qualifying],
        tolerance,
        axis="y",
    )
    assert clustered is not None
    if clustered[0][0] - caption_bbox[3] > window:
        return None
    max_gap = float(policy["table_light_max_row_gap_pt"])
    kept: list[tuple[float, list[tuple[float, float]]]] = [clustered[0]]
    for previous, current in pairwise(clustered):
        if current[0] - previous[0] > max_gap:
            break  # rules this far down belong to something else entirely.
        kept.append(current)
    if len(kept) < 2:
        return None
    kept_rules = [
        item
        for item in qualifying
        if any(abs(item.bbox[1] - y) <= tolerance for y, _ in kept)
    ]
    y_bounds = tuple(y for y, _ in kept)
    left = min(item.bbox[0] for item in kept_rules)
    right = max(item.bbox[2] for item in kept_rules)
    if _vertical_rules_inside(page_objects, y_bounds[0], y_bounds[-1], left, right, tolerance):
        return None
    x_bounds = _light_grid_bounds(
        page_objects, y_bounds, left, right, tolerance, policy
    )
    if x_bounds is None or len(x_bounds) < 3:
        return None
    grid = TableGrid(x_bounds, y_bounds, header_rows=1 if len(kept) >= 3 else 0)
    return LightGridPlan(grid, tuple(kept_rules))


def _thin_horizontal(bbox: list[float], tolerance: float) -> bool:
    x0, y0, x1, y1 = bbox
    return (y1 - y0) <= tolerance and (x1 - x0) > tolerance * 2


def _vertical_rules_inside(
    page_objects: list[Any],
    top: float,
    bottom: float,
    left: float,
    right: float,
    tolerance: float,
) -> bool:
    """True when any sizeable vertical rule crosses the candidate band.

    Vertical structure needs the boxed reconstruction path (or later span
    support); promoting it here would guess at merged columns.
    """
    pad = tolerance * 2
    minimum_height = max(tolerance * 2, 3.0)
    rule_thickness = max(tolerance, 1.0)
    for item in page_objects:
        x0, y0, x1, y1 = item.bbox
        width, height = x1 - x0, y1 - y0
        if not _rule_like(item, rule_thickness):
            continue
        if width > tolerance or height <= tolerance * 2 or height < minimum_height:
            continue
        if (
            y1 >= top - tolerance
            and y0 <= bottom + tolerance
            and left - pad <= (x0 + x1) / 2 <= right + pad
        ):
            return True
    return False


def _light_grid_bounds(
    page_objects: list[Any],
    y_bounds: tuple[float, ...],
    left: float,
    right: float,
    tolerance: float,
    policy: dict[str, Any],
) -> tuple[float, ...] | None:
    """Outer plus interior column bounds; ``None`` whenever shape is unsure.

    The band is trimmed at the bottom while its last row holds no characters
    (decorative hairlines beneath the bottom rule), but an interior empty row
    or an empty inferred column refuses promotion instead of being dropped.
    """
    pool = _light_band_chars(page_objects, y_bounds, left, right, tolerance)
    trimmed = list(y_bounds)
    while len(trimmed) >= 2 and not _band_has_chars(pool, trimmed[-2], trimmed[-1]):
        trimmed.pop()
    if len(trimmed) != len(y_bounds) and len(trimmed) < 2:
        return None
    if len(trimmed) == len(y_bounds):
        # Interior rows must each carry content; an empty interior band means
        # we misread decoration as structure.
        for low, high in pairwise(trimmed):
            if not _band_has_chars(pool, low, high):
                return None
    y_final = tuple(trimmed)
    vertical_pool = _light_band_chars(page_objects, y_final, left, right, tolerance)
    if len(vertical_pool) < 4:
        return None
    sizes = [float(item.attrs.get("size", 0.0)) or 8.0 for item in vertical_pool]
    split_min = median(sizes) * float(policy["table_light_column_gap_factor"])
    merged: list[list[float]] = []
    for start, end in sorted((item.bbox[0], item.bbox[2]) for item in vertical_pool):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    boundaries = [
        (previous[1] + following[0]) / 2
        for previous, following in pairwise(merged)
        if following[0] - previous[1] >= split_min
    ]
    if not boundaries:
        return None
    edges = [left, *boundaries, right]
    for index in range(len(edges) - 1):
        low, high = edges[index], edges[index + 1]
        if not any(
            low + 0.01 <= (item.bbox[0] + item.bbox[2]) / 2 <= high - 0.01
            for item in vertical_pool
        ):
            return None
    return (left, *boundaries, right)


def _light_band_chars(
    page_objects: list[Any],
    y_bounds: tuple[float, ...],
    left: float,
    right: float,
    tolerance: float,
) -> list[Any]:
    return [
        item
        for item in page_objects
        if item.kind == "char"
        and item.payload
        and not item.payload.isspace()
        and left - tolerance <= item.bbox[0]
        and item.bbox[2] <= right + tolerance
        and y_bounds[0] - tolerance <= item.bbox[1]
        and item.bbox[3] <= y_bounds[-1] + tolerance
    ]


def _band_has_chars(pool: list[Any], low: float, high: float) -> bool:
    return any(low <= (item.bbox[1] + item.bbox[3]) / 2 <= high for item in pool)
