"""Backend-neutral observations from a PDF source."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class RawPdfObject:
    object_ref: str
    kind: str
    page: int
    bbox: list[float]
    payload: str
    attrs: dict[str, Any]
    backend_ref: str


@dataclass(frozen=True)
class PdfPageObservation:
    page: int
    width: float
    height: float
    media_box: list[float]
    crop_box: list[float]
    rotation: int
    objects: list[RawPdfObject]


@dataclass(frozen=True)
class PdfBackendRun:
    name: str
    version: str
    options: dict[str, Any]
    pages: list[PdfPageObservation]


class PdfBackend(Protocol):
    name: str
    version: str

    def extract(self, source: Path, policy: dict[str, Any]) -> PdfBackendRun: ...
