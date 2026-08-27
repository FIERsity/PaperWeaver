"""Conservative text-layer recovery for simple display equations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from statistics import median
from typing import Any


@dataclass(frozen=True)
class EquationCandidate:
    object_refs: tuple[str, ...]
    bbox: tuple[float, float, float, float]
    raw_text: str
    latex: str | None
    number: str | None
    verified: bool


def detect_equations(page_objects: list[Any], page_width: float) -> list[EquationCandidate]:
    chars = [
        item
        for item in page_objects
        if item.kind == "char" and item.payload and not item.payload.isspace()
    ]
    candidates: list[EquationCandidate] = []
    consumed: set[str] = set()
    for equals in (item for item in chars if item.payload == "="):
        if equals.object_ref in consumed:
            continue
        base_y = _center_y(equals)
        base_size = float(equals.attrs.get("size", 0.0)) or 8.0
        number_chars, number = _number(chars, base_y, page_width)
        base = [
            item
            for item in chars
            if item.object_ref not in {value.object_ref for value in number_chars}
            and abs(_center_y(item) - base_y) <= base_size
        ]
        if not base:
            continue
        main = [item for item in base if abs(_center_y(item) - base_y) <= base_size * 0.62]
        if equals not in main:
            main.append(equals)
        min_x = min(item.bbox[0] for item in main)
        max_x = max(item.bbox[2] for item in main)
        main = [
            item
            for item in base
            if min_x - 2 <= item.bbox[0] and item.bbox[2] <= max_x + 2
        ]
        continuation_y = _continuation_y(chars, base_y, min_x, base_size)
        continuation: list[Any] = []
        if continuation_y is not None:
            continuation = [
                item
                for item in chars
                if abs(_center_y(item) - continuation_y) <= base_size
                and item.bbox[0] >= min_x - 2
                and item.object_ref not in {value.object_ref for value in number_chars}
            ]
        body = _unique(main + continuation)
        if not _looks_mathematical(body):
            continue
        rows = [main]
        if continuation:
            rows.append(continuation)
        latex_rows = [_latex_row(row, base_size) for row in rows]
        latex = " ".join(value for value in latex_rows if value) if all(latex_rows) else ""
        refs = _unique(body + number_chars)
        verified = bool(latex) and latex_balanced(latex) and not any(
            re.fullmatch(r"\(cid:\d+\)", item.payload) for item in refs
        )
        bbox = _union_bbox([item.bbox for item in refs])
        raw = "\n".join(_plain_row(row) for row in rows)
        candidate = EquationCandidate(
            tuple(item.object_ref for item in refs),
            tuple(bbox),
            raw,
            latex if verified else None,
            number,
            verified,
        )
        candidates.append(candidate)
        consumed.update(candidate.object_refs)
    return candidates


def _number(chars: list[Any], base_y: float, page_width: float) -> tuple[list[Any], str | None]:
    right = [
        item
        for item in chars
        if item.bbox[0] >= page_width * 0.72
        and base_y - 8 <= _center_y(item) <= base_y + 32
    ]
    groups = _rows(right, tolerance=2.5)
    matches = []
    for row in groups:
        text = "".join(item.payload for item in sorted(row, key=lambda value: value.bbox[0]))
        match = re.fullmatch(r"\((\d+[a-z]?)\)", text)
        if match:
            matches.append((abs(median(_center_y(item) for item in row) - base_y), row, match[1]))
    if not matches:
        return [], None
    _, row, number = min(matches, key=lambda value: value[0])
    return row, number


def _continuation_y(
    chars: list[Any], base_y: float, min_x: float, base_size: float
) -> float | None:
    plus = [
        item
        for item in chars
        if item.payload in {"+", "−", "-"}
        and base_y + base_size * 0.65 < _center_y(item) <= base_y + base_size * 2.5
        and item.bbox[0] <= min_x + 20
    ]
    return min((_center_y(item) for item in plus), default=None)


def _latex_row(chars: list[Any], base_size: float) -> str | None:
    ordered = sorted(_unique(chars), key=lambda item: (item.bbox[0], item.bbox[1]))
    converted = {item.object_ref: latex_char(item.payload) for item in ordered}
    if any(value is None for value in converted.values()):
        return None
    scripts = [item for item in ordered if float(item.attrs.get("size", 0.0)) < base_size * 0.78]
    base = [item for item in ordered if item not in scripts]
    output = ""
    used_scripts: set[str] = set()
    previous: Any = None
    for index, item in enumerate(base):
        value = converted[item.object_ref]
        assert value is not None
        if previous is not None:
            gap = item.bbox[0] - previous.bbox[2]
            if gap > max(1.5, base_size * 0.28) and value not in {")", "]", ","}:
                output += " "
        output += value
        next_x = base[index + 1].bbox[0] if index + 1 < len(base) else float("inf")
        attached = [
            script
            for script in scripts
            if script.object_ref not in used_scripts
            and item.bbox[2] - 0.6 <= script.bbox[0] < next_x
        ]
        if attached:
            attached.sort(key=lambda value: value.bbox[0])
            marker = "^" if median(_center_y(value) for value in attached) < _center_y(item) - 1 else "_"
            output += marker + "{" + "".join(
                converted[value.object_ref] or "" for value in attached
            ) + "}"
            used_scripts.update(value.object_ref for value in attached)
        previous = item
    output = re.sub(r"\s*([+=])\s*", r" \1 ", output)
    output = re.sub(r"\s*(\\cdot|\\times)\s*", r" \1 ", output)
    output = re.sub(r"\s+([)\],])", r"\1", output)
    output = re.sub(r"([(\[])\s+", r"\1", output)
    return re.sub(r"\s+", " ", output).strip()


def latex_char(value: str) -> str | None:
    mapped = {
        "α": r"\alpha",
        "β": r"\beta",
        "γ": r"\gamma",
        "δ": r"\delta",
        "ε": r"\epsilon",
        "η": r"\eta",
        "θ": r"\theta",
        "λ": r"\lambda",
        "μ": r"\mu",
        "π": r"\pi",
        "ρ": r"\rho",
        "σ": r"\sigma",
        "τ": r"\tau",
        "φ": r"\phi",
        "Δ": r"\Delta",
        "⋅": r"\cdot",
        "×": r"\times",
        "−": "-",
        "≤": r"\le",
        "≥": r"\ge",
    }.get(value)
    if mapped is not None:
        return mapped
    if re.fullmatch(r"[A-Za-z0-9=+\-*/().,\[\]:|'<>]", value):
        return value
    return None


def _looks_mathematical(chars: list[Any]) -> bool:
    if len(chars) < 5 or not any(item.payload == "=" for item in chars):
        return False
    mathish = sum(
        "italic" in str(item.attrs.get("fontname", "")).casefold()
        or "math" in str(item.attrs.get("fontname", "")).casefold()
        or "stix" in str(item.attrs.get("fontname", "")).casefold()
        or item.payload in {"=", "+", "−", "-", "⋅", "×"}
        for item in chars
    )
    return mathish / len(chars) >= 0.55


def _plain_row(chars: list[Any]) -> str:
    return "".join(item.payload for item in sorted(_unique(chars), key=lambda item: item.bbox[0]))


def _rows(chars: list[Any], tolerance: float) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for item in sorted(chars, key=lambda value: (value.bbox[1], value.bbox[0])):
        target = next(
            (
                row
                for row in reversed(rows[-4:])
                if abs(median(value.bbox[1] for value in row) - item.bbox[1]) <= tolerance
            ),
            None,
        )
        if target is None:
            rows.append([item])
        else:
            target.append(item)
    return rows


def _unique(chars: list[Any]) -> list[Any]:
    seen: set[str] = set()
    return [item for item in chars if not (item.object_ref in seen or seen.add(item.object_ref))]


def _center_y(item: Any) -> float:
    return (item.bbox[1] + item.bbox[3]) / 2


def latex_balanced(value: str) -> bool:
    stack: list[str] = []
    pairs = {")": "(", "]": "[", "}": "{"}
    for character in value:
        if character in "([{":
            stack.append(character)
        elif character in pairs and (not stack or stack.pop() != pairs[character]):
            return False
    return not stack


def _union_bbox(boxes: list[list[float]]) -> list[float]:
    return [
        round(min(box[0] for box in boxes), 4),
        round(min(box[1] for box in boxes), 4),
        round(max(box[2] for box in boxes), 4),
        round(max(box[3] for box in boxes), 4),
    ]
