"""Canonical pdfplumber observations; no reading-order decisions live here."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

from .pdf_backend import PdfBackendRun, PdfPageObservation, RawPdfObject
from .pdf_contracts import PdfUnsupportedError, make_object_ref, validate_bbox

# A subsetted font carries a six-letter prefix (e.g. KFCUEC+) before the base name.
_SUBSET_PREFIX = re.compile(r"^[A-Za-z]{6}\+")

# Computer Modern maths-symbols fonts (LaTeX) expose an unmapped ``cid:0`` glyph for
# the U+2212 MINUS SIGN. pdfplumber cannot recover it because the embedded subset omits
# the ToUnicode entry, so it degrades to ``(cid:0)``. We recover it from font identity,
# corroborated by the glyph's advance width: a minus is a short bar, ~0.25x the point
# size, never a full-height box or a wide symbol.
_TEX_MATH_SYMBOLS_FONT = "TeX_CM_Maths_Symbols"
_MINUS_SIGN = "−"


class PdfPlumberBackend:
    name = "pdfplumber"

    def __init__(self) -> None:
        try:
            import pdfplumber
        except ImportError as error:
            raise RuntimeError(
                "PDF_BACKEND_MISSING: Install paperweaver[pdf] to import PDF sources."
            ) from error
        self._pdfplumber = pdfplumber
        self.version = pdfplumber.__version__

    def extract(self, source: Path, policy: dict[str, Any]) -> PdfBackendRun:
        options = {"unicode_norm": None, "laparams": None, "cropbox": True}
        pages: list[PdfPageObservation] = []
        leaf_count = 0
        try:
            document_context = self._pdfplumber.open(source, unicode_norm=None)
        except Exception as error:
            raise ValueError(f"PDF_CORRUPT: unable to parse PDF: {error}") from error
        with document_context as document:
            if not document.pages:
                raise ValueError("PDF_EMPTY: PDF contains no pages")
            if len(document.pages) > policy["max_pages"]:
                raise PdfUnsupportedError(
                    f"PDF_RESOURCE_LIMIT: {len(document.pages)} pages exceeds "
                    f"{policy['max_pages']}"
                )
            for page_number, page in enumerate(document.pages, 1):
                rotation = int(page.rotation or 0) % 360
                media_box = [float(value) for value in page.mediabox]
                native_crop_box = [
                    float(value) for value in (page.cropbox or page.mediabox)
                ]
                if rotation == 0:
                    width = native_crop_box[2] - native_crop_box[0]
                    height = native_crop_box[3] - native_crop_box[1]
                    x_offset = native_crop_box[0] - media_box[0]
                    y_offset = native_crop_box[1] - media_box[1]
                else:
                    width, height = float(page.width), float(page.height)
                    x_offset = y_offset = 0.0
                objects: list[RawPdfObject] = []
                occurrences: Counter[tuple[str, tuple[float, ...], str]] = Counter()
                sources = (
                    ("char", page.chars),
                    ("image_occurrence", page.images),
                    ("line", page.lines),
                    ("rect", page.rects),
                    ("curve", page.curves),
                    ("annotation", page.annots or []),
                )
                for kind, rows in sources:
                    for native_index, row in enumerate(rows):
                        native_bbox = _bbox(row)
                        bbox = [
                            native_bbox[0] - x_offset,
                            native_bbox[1] - y_offset,
                            native_bbox[2] - x_offset,
                            native_bbox[3] - y_offset,
                        ]
                        bbox = _clip_bbox(
                            bbox, width, height, preserve_outside=rotation != 0
                        )
                        if bbox is None:
                            continue
                        validate_bbox(bbox, width, height)
                        payload = _payload(kind, row)
                        key = (kind, tuple(round(item, 3) for item in bbox), payload)
                        occurrences[key] += 1
                        object_ref = make_object_ref(
                            page_number, kind, bbox, payload, occurrences[key]
                        )
                        attrs = _attrs(kind, row)
                        attrs["native_bbox"] = native_bbox
                        objects.append(
                            RawPdfObject(
                                object_ref,
                                kind,
                                page_number,
                                bbox,
                                payload,
                                attrs,
                                f"pages/{page_number}/objects/{kind}/{native_index}",
                            )
                        )
                leaf_count += len(objects)
                if leaf_count > policy["max_leaf_objects"]:
                    raise PdfUnsupportedError(
                        f"PDF_RESOURCE_LIMIT: leaf objects exceed "
                        f"{policy['max_leaf_objects']}"
                    )
                pages.append(
                    PdfPageObservation(
                        page_number,
                        width,
                        height,
                        media_box,
                        native_crop_box,
                        rotation,
                        objects,
                    )
                )
        return PdfBackendRun(self.name, self.version, options, pages)


def _bbox(row: dict[str, Any]) -> list[float]:
    x0 = float(row.get("x0", 0.0))
    x1 = float(row.get("x1", x0))
    top = float(row.get("top", 0.0))
    bottom = float(row.get("bottom", top))
    if x1 <= x0:
        x1 = x0 + max(float(row.get("linewidth", 0.01)), 0.01)
    if bottom <= top:
        bottom = top + max(float(row.get("linewidth", 0.01)), 0.01)
    return [round(x0, 4), round(top, 4), round(x1, 4), round(bottom, 4)]


def _payload(kind: str, row: dict[str, Any]) -> str:
    if kind == "char":
        return _recover_unmapped_glyph(row)
    if kind == "image_occurrence":
        return f"image:{row.get('srcsize')}:{row.get('bits')}:{row.get('colorspace')}"
    return kind


def _recover_unmapped_glyph(row: dict[str, Any]) -> str:
    """Restore a trustworthy Unicode for a char pdfplumber left as ``(cid:N)``.

    Callers must never guess a glyph from a general dictionary; recovery is only
    performed when the font identity *and* the glyph geometry jointly pin down a
    single character. Unrecovered glyphs stay as ``(cid:N)`` so downstream stages
    can flag them as unresolved.
    """
    text = str(row.get("text", ""))
    if not text.startswith("(cid:"):
        return text
    base = _SUBSET_PREFIX.sub("", str(row.get("fontname", "")))
    if base != _TEX_MATH_SYMBOLS_FONT:
        return text
    width = float(row.get("width")) if row.get("width") is not None else (
        float(row.get("x1", 0.0)) - float(row.get("x0", 0.0))
    )
    size = float(row.get("size", 0.0))
    if 0 < width < size * 0.45:
        return _MINUS_SIGN
    return text


def _clip_bbox(
    bbox: list[float], width: float, height: float, *, preserve_outside: bool
) -> list[float] | None:
    x0, y0, x1, y1 = bbox
    clipped = [max(0.0, x0), max(0.0, y0), min(width, x1), min(height, y1)]
    if clipped[2] <= clipped[0] or clipped[3] <= clipped[1]:
        if not preserve_outside:
            return None
        x = min(max((x0 + x1) / 2, 0.01), max(width - 0.01, 0.01))
        y = min(max((y0 + y1) / 2, 0.01), max(height - 0.01, 0.01))
        clipped = [max(0.0, x - 0.005), max(0.0, y - 0.005), min(width, x + 0.005), min(height, y + 0.005)]
    return [round(value, 4) for value in clipped]


def _attrs(kind: str, row: dict[str, Any]) -> dict[str, Any]:
    if kind == "char":
        return {
            "fontname": str(row.get("fontname", "")),
            "size": round(float(row.get("size", 0.0)), 4),
            "upright": bool(row.get("upright", True)),
            "matrix": list(row.get("matrix", ())),
            "stroking_color": _json_value(row.get("stroking_color")),
            "non_stroking_color": _json_value(row.get("non_stroking_color")),
        }
    if kind == "image_occurrence":
        return {
            "srcsize": list(row.get("srcsize", ())),
            "bits": _json_value(row.get("bits")),
            "colorspace": _json_value(row.get("colorspace")),
        }
    return {"linewidth": _json_value(row.get("linewidth"))}


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return str(value)
