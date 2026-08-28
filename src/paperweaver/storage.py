"""Strict, transparent JSON and JSONL persistence."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import fields
from pathlib import Path
from typing import Any, TypeVar

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
    atomic_write_text(
        path,
        "".join(json.dumps(row.to_dict(), ensure_ascii=False) + "\n" for row in rows),
    )


def append_jsonl(path: Path, row: Record) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row.to_dict(), ensure_ascii=False) + "\n")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    os.replace(temporary, path)


def atomic_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    os.replace(temporary, path)


def write_dict_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    atomic_write_text(
        path,
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in rows),
    )


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
