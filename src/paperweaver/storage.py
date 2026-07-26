"""Strict, transparent JSON and JSONL persistence."""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path
from typing import TypeVar

from .models import Record

T = TypeVar("T", bound=Record)


def read_jsonl(path: Path, cls: type[T]) -> list[T]:
    if not path.exists():
        return []
    names = {field.name for field in fields(cls)}
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        raw = json.loads(line)
        unknown = set(raw) - names
        if unknown:
            raise ValueError(f"{path}:{number}: unknown fields {sorted(unknown)}")
        rows.append(cls(**raw))
    return rows


def write_jsonl(path: Path, rows: list[Record]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row.to_dict(), ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def append_jsonl(path: Path, row: Record) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row.to_dict(), ensure_ascii=False) + "\n")
