#!/usr/bin/env python3
"""Bootstrap and run the checksum-pinned open PDF regression corpus."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import sys
import tempfile
import time
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPOSITORY_ROOT / "tests" / "corpus" / "pdf-jats-manifest.json"
DEFAULT_CACHE = REPOSITORY_ROOT / "tmp" / "corpus-cache"
DEFAULT_RUNS = REPOSITORY_ROOT / "tmp" / "corpus-runs"
DEFAULT_FLOORS = REPOSITORY_ROOT / "tests" / "corpus" / "semantic-floors.json"
USER_AGENT = "PaperWeaver/0.5 open-paper regression corpus"
ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
RUN_ID_PATTERN = re.compile(r"^[0-9]{8}T[0-9]{6}Z(?:-[a-z0-9-]+)?$")

ROOT_KEYS = {"schema_version", "corpus_id", "description", "papers"}
PAPER_KEYS = {
    "id",
    "title",
    "year",
    "repository",
    "landing_url",
    "license",
    "layout_tags",
    "files",
    "expected",
}
LICENSE_KEYS = {"identifier", "url", "evidence_url"}
FILE_KEYS = {"url", "sha256", "size_bytes", "page_count"}
EXPECTED_KEYS = {
    "jats_elements",
    "semantic_targets",
    "required_status",
    "approved_differences",
}
JATS_ELEMENT_KEYS = {"figures", "tables", "equations", "references"}
SEMANTIC_KEYS = {"recall", "precision", "order_ratio"}


class CorpusError(RuntimeError):
    """A stable, user-facing corpus bootstrap or run error."""


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CorpusError(f"CORPUS_MANIFEST_INVALID: {error}") from error
    _require_exact_keys(manifest, ROOT_KEYS, "manifest")
    if manifest["schema_version"] != 2:
        raise CorpusError("CORPUS_MANIFEST_INVALID: schema_version must be 2")
    if not ID_PATTERN.fullmatch(manifest["corpus_id"]):
        raise CorpusError("CORPUS_MANIFEST_INVALID: corpus_id is not a stable slug")
    if not isinstance(manifest["description"], str) or not manifest["description"].strip():
        raise CorpusError("CORPUS_MANIFEST_INVALID: description is empty")
    papers = manifest["papers"]
    if not isinstance(papers, list) or not papers:
        raise CorpusError("CORPUS_MANIFEST_INVALID: papers must be a non-empty list")
    ids: set[str] = set()
    for paper in papers:
        _validate_paper(paper)
        if paper["id"] in ids:
            raise CorpusError(f"CORPUS_MANIFEST_INVALID: duplicate paper id {paper['id']}")
        ids.add(paper["id"])
    return manifest


def _validate_paper(paper: dict[str, Any]) -> None:
    _require_exact_keys(paper, PAPER_KEYS, "paper")
    if not ID_PATTERN.fullmatch(paper["id"]):
        raise CorpusError("CORPUS_MANIFEST_INVALID: paper id is not a stable slug")
    for name in ("title", "repository", "landing_url"):
        if not isinstance(paper[name], str) or not paper[name].strip():
            raise CorpusError(f"CORPUS_MANIFEST_INVALID: {paper['id']} has invalid {name}")
    if not isinstance(paper["year"], int) or not 1600 <= paper["year"] <= 2100:
        raise CorpusError(f"CORPUS_MANIFEST_INVALID: {paper['id']} has invalid year")
    if not paper["landing_url"].startswith("https://"):
        raise CorpusError(f"CORPUS_MANIFEST_INVALID: {paper['id']} landing_url must use HTTPS")
    _require_exact_keys(paper["license"], LICENSE_KEYS, f"{paper['id']} license")
    if not all(
        isinstance(paper["license"][key], str) and paper["license"][key].strip()
        for key in LICENSE_KEYS
    ):
        raise CorpusError(f"CORPUS_MANIFEST_INVALID: {paper['id']} license is incomplete")
    tags = paper["layout_tags"]
    if not isinstance(tags, list) or not tags or len(tags) != len(set(tags)):
        raise CorpusError(f"CORPUS_MANIFEST_INVALID: {paper['id']} layout_tags are invalid")
    files = paper["files"]
    if not isinstance(files, dict) or not set(files) <= {"pdf", "jats"} or "pdf" not in files:
        raise CorpusError(f"CORPUS_MANIFEST_INVALID: {paper['id']} files are invalid")
    for kind, spec in files.items():
        _validate_file_spec(paper["id"], kind, spec)
    _require_exact_keys(paper["expected"], EXPECTED_KEYS, f"{paper['id']} expected")
    expected = paper["expected"]
    if set(expected["jats_elements"]) - JATS_ELEMENT_KEYS:
        raise CorpusError(f"CORPUS_MANIFEST_INVALID: {paper['id']} JATS element key is invalid")
    if "jats" in files and set(expected["jats_elements"]) != JATS_ELEMENT_KEYS:
        raise CorpusError(f"CORPUS_MANIFEST_INVALID: {paper['id']} needs all JATS counts")
    if any(not isinstance(value, int) or value < 0 for value in expected["jats_elements"].values()):
        raise CorpusError(f"CORPUS_MANIFEST_INVALID: {paper['id']} JATS counts are invalid")
    if not isinstance(expected["approved_differences"], list) or any(
        not isinstance(value, str) or not value.strip()
        for value in expected["approved_differences"]
    ):
        raise CorpusError(f"CORPUS_MANIFEST_INVALID: {paper['id']} differences are invalid")
    targets = expected["semantic_targets"]
    if "jats" in files:
        if not isinstance(targets, dict) or set(targets) != SEMANTIC_KEYS:
            raise CorpusError(f"CORPUS_MANIFEST_INVALID: {paper['id']} semantic targets are missing")
        if any(not isinstance(value, (int, float)) or not 0 <= value <= 1 for value in targets.values()):
            raise CorpusError(f"CORPUS_MANIFEST_INVALID: {paper['id']} semantic targets are invalid")
    elif targets is not None:
        raise CorpusError(f"CORPUS_MANIFEST_INVALID: {paper['id']} has no JATS semantic oracle")
    if expected["required_status"] not in {None, "complete"}:
        raise CorpusError(f"CORPUS_MANIFEST_INVALID: {paper['id']} status gate is invalid")


def _validate_file_spec(paper_id: str, kind: str, spec: dict[str, Any]) -> None:
    expected_keys = FILE_KEYS if kind == "pdf" else FILE_KEYS - {"page_count"}
    _require_exact_keys(spec, expected_keys, f"{paper_id} {kind}")
    if not spec["url"].startswith("https://"):
        raise CorpusError(f"CORPUS_MANIFEST_INVALID: {paper_id} {kind} URL must use HTTPS")
    if not SHA256_PATTERN.fullmatch(spec["sha256"]):
        raise CorpusError(f"CORPUS_MANIFEST_INVALID: {paper_id} {kind} SHA-256 is invalid")
    if not isinstance(spec["size_bytes"], int) or spec["size_bytes"] <= 0:
        raise CorpusError(f"CORPUS_MANIFEST_INVALID: {paper_id} {kind} size is invalid")
    if kind == "pdf" and (not isinstance(spec["page_count"], int) or spec["page_count"] <= 0):
        raise CorpusError(f"CORPUS_MANIFEST_INVALID: {paper_id} page count is invalid")


def _require_exact_keys(value: Any, expected: set[str], context: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise CorpusError(
            f"CORPUS_MANIFEST_INVALID: {context} keys must be " + ", ".join(sorted(expected))
        )


def select_papers(
    manifest: dict[str, Any], ids: set[str] | None = None, tags: set[str] | None = None
) -> list[dict[str, Any]]:
    papers = manifest["papers"]
    known = {paper["id"] for paper in papers}
    if ids and not ids <= known:
        raise CorpusError("CORPUS_SELECTION_INVALID: unknown ids: " + ", ".join(sorted(ids - known)))
    selected = [
        paper
        for paper in papers
        if (not ids or paper["id"] in ids)
        and (not tags or tags <= set(paper["layout_tags"]))
    ]
    if not selected:
        raise CorpusError("CORPUS_SELECTION_EMPTY: no papers matched the selection")
    return selected


def cached_path(cache_root: Path, paper_id: str, kind: str) -> Path:
    return cache_root / paper_id / ("paper.pdf" if kind == "pdf" else "paper.xml")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_cached_file(path: Path, spec: dict[str, Any], kind: str) -> None:
    if not path.exists():
        raise CorpusError(f"CORPUS_CACHE_MISSING: {path}")
    size = path.stat().st_size
    if size != spec["size_bytes"]:
        raise CorpusError(
            f"CORPUS_SOURCE_SIZE_MISMATCH: {path} has {size}, expected {spec['size_bytes']}"
        )
    digest = sha256_file(path)
    if digest != spec["sha256"]:
        raise CorpusError(
            f"CORPUS_SOURCE_DIGEST_MISMATCH: {path} has {digest}, expected {spec['sha256']}"
        )
    with path.open("rb") as handle:
        prefix = handle.read(16).lstrip()
    if kind == "pdf" and not prefix.startswith(b"%PDF-"):
        raise CorpusError(f"CORPUS_SOURCE_FORMAT_MISMATCH: {path} is not a PDF")
    if kind == "jats" and not prefix.startswith(b"<"):
        raise CorpusError(f"CORPUS_SOURCE_FORMAT_MISMATCH: {path} is not XML")


def fetch_file(path: Path, spec: dict[str, Any], kind: str) -> str:
    if path.exists():
        verify_cached_file(path, spec, kind)
        return "cached"
    path.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(spec["url"], headers={"User-Agent": USER_AGENT})
    temporary_path: Path | None = None
    try:
        with (
            urllib.request.urlopen(request, timeout=120) as response,
            tempfile.NamedTemporaryFile(
                dir=path.parent, prefix=".download-", delete=False
            ) as out,
        ):
            temporary_path = Path(out.name)
            _copy_limited(response, out, spec["size_bytes"])
        verify_cached_file(temporary_path, spec, kind)
        os.replace(temporary_path, path)
    except CorpusError:
        raise
    except Exception as error:
        raise CorpusError(f"CORPUS_FETCH_FAILED: {spec['url']}: {error}") from error
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()
    return "downloaded"


def _copy_limited(source: BinaryIO, destination: BinaryIO, expected_size: int) -> None:
    total = 0
    while chunk := source.read(1024 * 1024):
        total += len(chunk)
        if total > expected_size:
            raise CorpusError("CORPUS_SOURCE_SIZE_MISMATCH: download exceeded pinned size")
        destination.write(chunk)


def verify_jats_oracle(path: Path, expected: dict[str, int]) -> dict[str, int]:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as error:
        raise CorpusError(f"CORPUS_JATS_INVALID: {path}: {error}") from error
    names = Counter(element.tag.rsplit("}", 1)[-1] for element in root.iter())
    actual = {
        "figures": names["fig"],
        "tables": names["table-wrap"],
        "equations": names["disp-formula"],
        "references": names["ref"],
    }
    if actual != expected:
        raise CorpusError(f"CORPUS_JATS_ORACLE_MISMATCH: {path}: {actual} != {expected}")
    return actual


def diagnostic_tokens(text: str) -> list[str]:
    """Tokenize multilingual paper text for non-gating corpus diagnostics."""
    text = unicodedata.normalize("NFC", text).translate(
        str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl"})
    )
    tokens: list[str] = []
    buffer = ""
    mode = ""
    for character in text:
        category = unicodedata.category(character)
        cjk = _is_cjk(character)
        current = "cjk" if cjk else "word" if category[0] in {"L", "M"} else "number" if category[0] == "N" else "space" if character.isspace() else "symbol"
        if current in {"cjk", "symbol"}:
            if buffer:
                tokens.append(buffer)
                buffer = ""
            tokens.append(character)
            mode = ""
        elif current == "space":
            if buffer:
                tokens.append(buffer)
                buffer = ""
            mode = ""
        elif current == mode:
            buffer += character
        else:
            if buffer:
                tokens.append(buffer)
            buffer = character
            mode = current
    if buffer:
        tokens.append(buffer)
    return tokens


def token_diagnostics(reference: list[str], candidate: list[str]) -> dict[str, Any]:
    overlap = sum((Counter(reference) & Counter(candidate)).values())
    ordered = lcs_length(reference, candidate)
    return {
        "method": "unicode-token-shortest-edit-v1",
        "reference_tokens": len(reference),
        "candidate_tokens": len(candidate),
        "multiset_overlap_tokens": overlap,
        "aligned_tokens": ordered,
        "recall": ordered / len(reference) if reference else 1.0,
        "precision": ordered / len(candidate) if candidate else 1.0,
        "order_ratio": ordered / overlap if overlap else 1.0,
        "gating": True,
    }


def partitioned_diagnostics(
    reference: dict[str, list[str]], candidate: dict[str, list[str]]
) -> dict[str, Any]:
    """Score prose and table streams separately, then aggregate honestly.

    Table text is aligned against table text and prose against prose, so
    promoting an unstructured region into a verified table can never look
    like a recall regression merely because tokens changed streams.
    """
    partitions: dict[str, dict[str, Any]] = {}
    totals = {"reference_tokens": 0, "candidate_tokens": 0, "multiset_overlap_tokens": 0, "aligned_tokens": 0}
    for name in ("prose", "table"):
        diagnostics = token_diagnostics(reference[name], candidate[name])
        partitions[name] = diagnostics
        for key in totals:
            totals[key] += diagnostics[key]
    reference_total = totals["reference_tokens"]
    candidate_total = totals["candidate_tokens"]
    return {
        "method": "unicode-token-partitioned-v2",
        "reference_tokens": reference_total,
        "candidate_tokens": candidate_total,
        "multiset_overlap_tokens": totals["multiset_overlap_tokens"],
        "aligned_tokens": totals["aligned_tokens"],
        "recall": totals["aligned_tokens"] / reference_total if reference_total else 1.0,
        "precision": totals["aligned_tokens"] / candidate_total if candidate_total else 1.0,
        "order_ratio": (
            totals["aligned_tokens"] / totals["multiset_overlap_tokens"]
            if totals["multiset_overlap_tokens"]
            else 1.0
        ),
        "partitions": partitions,
        "gating": True,
    }


def lcs_length(reference: list[str], candidate: list[str]) -> int:
    """Return exact LCS length using a deterministic bit-parallel algorithm."""
    positions: dict[str, int] = {}
    for index, token in enumerate(candidate):
        positions[token] = positions.get(token, 0) | (1 << index)
    state = 0
    for token in reference:
        matches = positions.get(token, 0)
        update = state | matches
        state = update & ~(update - ((state << 1) | 1))
    return state.bit_count()


def _is_cjk(character: str) -> bool:
    value = ord(character)
    return (
        0x3400 <= value <= 0x4DBF
        or 0x4E00 <= value <= 0x9FFF
        or 0xF900 <= value <= 0xFAFF
        or 0x20000 <= value <= 0x3134F
    )


def _jats_streams(path: Path) -> dict[str, str]:
    """Split the JATS body into prose and table-wrap text streams."""
    root = ET.parse(path).getroot()
    body = next((item for item in root.iter() if item.tag.rsplit("}", 1)[-1] == "body"), None)
    table_parts: list[str] = []

    def prose_text(node: ET.Element) -> str:
        parts = [node.text or ""]
        for child in node:
            if child.tag.rsplit("}", 1)[-1] == "table-wrap":
                table_parts.append(" ".join(child.itertext()))
            else:
                parts.append(prose_text(child))
            parts.append(child.tail or "")
        return " ".join(part for part in parts if part and part.strip())

    prose = prose_text(body) if body is not None else ""
    return {"prose": prose, "table": " ".join(table_parts)}


def _pdf_streams(project: Path) -> dict[str, str]:
    """Split the PDF body into prose and verified-table text streams."""
    manifest = json.loads(
        (project / "source" / "pdf" / "manifest.json").read_text(encoding="utf-8")
    )
    blocks_path = (
        project
        / "source"
        / "pdf"
        / "runs"
        / manifest["active_run_id"]
        / "base-blocks.jsonl"
    )
    blocks = [json.loads(line) for line in blocks_path.read_text(encoding="utf-8").splitlines()]
    prose_lines: list[str] = []
    table_lines: list[str] = []
    in_body = False
    stop_headings = {"acknowledgments", "acknowledgements", "references"}
    for block in blocks:
        kind = block["kind"]
        text = (block.get("text") or "").strip()
        if kind == "section_heading":
            heading = text.casefold()
            if heading == "introduction":
                in_body = True
            elif in_body and heading in stop_headings:
                break
        if not in_body:
            continue
        if kind in {"section_heading", "paragraph", "figure_caption", "table_caption"}:
            prose_lines.append(text)
        elif kind == "equation" and block["status"] == "ok":
            prose_lines.append((block.get("raw_text") or text).strip())
        elif kind == "table" and block["status"] == "ok" and block.get("table"):
            table_lines.extend(" ".join(row) for row in block["table"]["rows"])
    return {
        "prose": "\n".join(value for value in prose_lines if value),
        "table": "\n".join(value for value in table_lines if value),
    }


def environment_record() -> dict[str, Any]:
    packages = {}
    for distribution in ("paperweaver", "pdfplumber", "pypdfium2", "Pillow", "pypdf", "reportlab"):
        try:
            packages[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            packages[distribution] = None
    from paperweaver.pdf_contracts import load_policy

    _, policy_sha256 = load_policy()
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "packages": packages,
        "pdf_policy_sha256": policy_sha256,
    }


def load_floors(path: Path = DEFAULT_FLOORS) -> dict[str, dict[str, float]]:
    """Load the pinned per-paper semantic regression floors."""
    if not path.exists():
        raise CorpusError(
            f"CORPUS_FLOORS_MISSING: {path} (pin them first with `pdf_corpus.py pin-floors --from <run>`)"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CorpusError(f"CORPUS_FLOORS_INVALID: {error}") from error
    if not isinstance(data, dict) or set(data) != {
        "schema_version", "source_run_id", "pinned_at", "guard", "papers"
    }:
        raise CorpusError("CORPUS_FLOORS_INVALID: floors keys must be guard, papers, pinned_at, schema_version, source_run_id")
    if data["schema_version"] != 1:
        raise CorpusError("CORPUS_FLOORS_INVALID: schema_version must be 1")
    papers = data["papers"]
    if not isinstance(papers, dict) or not papers:
        raise CorpusError("CORPUS_FLOORS_INVALID: papers must be a non-empty object")
    for paper_id, floors in papers.items():
        if not ID_PATTERN.fullmatch(paper_id) or not isinstance(floors, dict):
            raise CorpusError(f"CORPUS_FLOORS_INVALID: {paper_id} entry is invalid")
        if set(floors) != SEMANTIC_KEYS or any(
            not isinstance(value, (int, float)) or not 0 <= value <= 1 for value in floors.values()
        ):
            raise CorpusError(f"CORPUS_FLOORS_INVALID: {paper_id} floors are invalid")
    return papers


def floor_gate(measured: dict[str, Any], floors: dict[str, float]) -> dict[str, Any]:
    """Compare measured aggregate semantics against pinned regression floors."""
    return {
        "floors": dict(floors),
        "passed": all(measured[name] >= floors[name] for name in SEMANTIC_KEYS),
    }


def run_paper(
    paper: dict[str, Any],
    cache_root: Path,
    run_root: Path,
    floors: dict[str, float] | None = None,
) -> dict[str, Any]:
    from paperweaver.core import import_paper, init_project
    from paperweaver.pdf_contracts import PdfUnsupportedError

    started = time.monotonic()
    project = run_root / "projects" / paper["id"]
    result: dict[str, Any] = {
        "id": paper["id"],
        "source_sha256": paper["files"]["pdf"]["sha256"],
        "status": "fatal",
        "duration_seconds": None,
        "error": None,
        "metrics": {},
        "issue_codes": {},
        "jats_elements": paper["expected"]["jats_elements"],
        "token_diagnostics": None,
        "semantic_gate": None,
        "status_gate": None,
        "idempotent": False,
    }
    try:
        source = cached_path(cache_root, paper["id"], "pdf")
        verify_cached_file(source, paper["files"]["pdf"], "pdf")
        if "jats" in paper["files"]:
            jats = cached_path(cache_root, paper["id"], "jats")
            verify_cached_file(jats, paper["files"]["jats"], "jats")
            verify_jats_oracle(jats, paper["expected"]["jats_elements"])
        init_project(project, paper["title"], "en", "zh-CN")
        imported = import_paper(project, source)
        repeated = import_paper(project, source)
        result["idempotent"] = imported == repeated
        qa = json.loads((project / "source" / "pdf" / "qa.json").read_text(encoding="utf-8"))
        result["status"] = qa["status"]
        required_status = paper["expected"]["required_status"]
        if required_status is not None:
            result["status_gate"] = {
                "required": required_status,
                "passed": qa["status"] in {"complete", "complete_with_warnings"},
            }
        result["metrics"] = qa["metrics"]
        result["issue_codes"] = dict(sorted(Counter(item["code"] for item in qa["issues"]).items()))
        if "jats" in paper["files"]:
            if floors is None:
                raise CorpusError(
                    f"CORPUS_FLOORS_MISSING: no pinned floors for {paper['id']}"
                )
            reference_streams = _jats_streams(jats)
            candidate_streams = _pdf_streams(project)
            result["token_diagnostics"] = partitioned_diagnostics(
                {name: diagnostic_tokens(text) for name, text in reference_streams.items()},
                {name: diagnostic_tokens(text) for name, text in candidate_streams.items()},
            )
            result["semantic_targets"] = paper["expected"]["semantic_targets"]
            result["semantic_gate"] = floor_gate(result["token_diagnostics"], floors)
    except (CorpusError, FileExistsError, FileNotFoundError, PdfUnsupportedError, RuntimeError, ValueError) as error:
        result["error"] = f"{type(error).__name__}: {error}"
    result["duration_seconds"] = round(time.monotonic() - started, 3)
    return result


def build_report(
    manifest: dict[str, Any], papers: list[dict[str, Any]], results: list[dict[str, Any]], run_id: str
) -> dict[str, Any]:
    statuses = Counter(result["status"] for result in results)
    semantic_failures = sum(
        item["semantic_gate"] is not None and not item["semantic_gate"]["passed"]
        for item in results
    )
    status_failures = sum(
        item["status_gate"] is not None and not item["status_gate"]["passed"]
        for item in results
    )
    return {
        "schema_version": 1,
        "corpus_id": manifest["corpus_id"],
        "run_id": run_id,
        "environment": environment_record(),
        "selection": [paper["id"] for paper in papers],
        "summary": {
            "papers": len(results),
            "statuses": dict(sorted(statuses.items())),
            "semantic_failures": semantic_failures,
            "status_failures": status_failures,
        },
        "results": sorted(results, key=lambda item: item["id"]),
    }


def _floor_cell(measured: dict[str, Any] | None, key: str) -> str:
    if not measured:
        return ""
    floors = measured.get("floors") or {}
    if not floors:
        return f"{measured[key]:.4f}"
    return f"{measured[key]:.4f} ({floors[key]:.4f})"


def render_report_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Open PDF corpus report",
        "",
        f"- Corpus: `{report['corpus_id']}`",
        f"- Run: `{report['run_id']}`",
        f"- Papers: `{report['summary']['papers']}`",
        f"- Statuses: `{json.dumps(report['summary']['statuses'], sort_keys=True)}`",
        f"- Semantic gate failures: `{report['summary']['semantic_failures']}`",
        f"- Status gate failures: `{report['summary']['status_failures']}`",
        "",
        "| Paper | Status | Pages | Figures | Tables | Equations | Unresolved | Recall (floor) | Precision (floor) | Order (floor) | Seconds |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in report["results"]:
        metrics = item["metrics"]
        diagnostics = item.get("token_diagnostics")
        lines.append(
            f"| `{item['id']}` | {item['status']} | {metrics.get('pages', '')} | "
            f"{metrics.get('verified_figures', '')} | {metrics.get('verified_tables', '')} | "
            f"{metrics.get('verified_equations', '')} | {metrics.get('unresolved_blocks', '')} | "
            f"{_floor_cell(diagnostics, 'recall')} | {_floor_cell(diagnostics, 'precision')} | "
            f"{_floor_cell(diagnostics, 'order_ratio')} | {item['duration_seconds']} |"
        )
        if item["error"]:
            lines.append(f"\nError for `{item['id']}`: `{item['error']}`\n")
    for item in report["results"]:
        diagnostics = item.get("token_diagnostics")
        targets = item.get("semantic_targets")
        if not diagnostics or not targets:
            continue
        gaps = [
            f"{name} {diagnostics[name]:.4f}/{targets[name]}"
            for name in sorted(SEMANTIC_KEYS)
            if diagnostics[name] < targets[name]
        ]
        if gaps:
            lines.append(f"\nTarget gap for `{item['id']}`: {', '.join(gaps)}\n")
    return "\n".join(lines) + "\n"


def pin_floors(args: argparse.Namespace) -> int:
    """Pin semantic floors from a finished run; the diff is reviewed in git."""
    report_path = args.source_run / "report.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CorpusError(f"CORPUS_PIN_REFUSED: cannot read {report_path}: {error}") from error
    if not isinstance(args.guard, (int, float)) or not 0 <= args.guard <= 0.05:
        raise CorpusError("CORPUS_PIN_REFUSED: --guard must be between 0 and 0.05")
    papers: dict[str, dict[str, float]] = {}
    for item in report.get("results", []):
        diagnostics = item.get("token_diagnostics")
        if not diagnostics:
            continue
        if item.get("error"):
            raise CorpusError(f"CORPUS_PIN_REFUSED: {item['id']} ended with an error")
        papers[item["id"]] = {
            name: max(0.0, round(diagnostics[name] - args.guard, 4)) for name in SEMANTIC_KEYS
        }
    if not papers:
        raise CorpusError("CORPUS_PIN_REFUSED: the run has no JATS semantic results")
    previous = load_floors(args.floors) if args.floors.exists() else {}
    for paper_id in sorted(papers):
        old_floors = previous.get(paper_id)
        new_floors = papers[paper_id]
        if old_floors:
            changes = ", ".join(
                f"{name} {old_floors[name]:.4f}->{new_floors[name]:.4f}" for name in sorted(SEMANTIC_KEYS)
            )
        else:
            changes = ", ".join(f"{name} {new_floors[name]:.4f}" for name in sorted(SEMANTIC_KEYS))
        print(f"pin {paper_id}: {changes}")
    payload = {
        "schema_version": 1,
        "source_run_id": report.get("run_id", "unknown"),
        "pinned_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "guard": args.guard,
        "papers": dict(sorted(papers.items())),
    }
    args.floors.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"pinned {len(papers)} papers -> {args.floors}")
    return 0


def _selection_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--id", action="append", default=[])
    command.add_argument("--tag", action="append", default=[])


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    root.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    commands = root.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch", help="Download missing files and verify pinned bytes")
    _selection_arguments(fetch)
    verify = commands.add_parser("verify", help="Verify the offline cache without network access")
    _selection_arguments(verify)
    run = commands.add_parser("run", help="Import cached PDFs and write an aggregate QA report")
    _selection_arguments(run)
    run.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
    run.add_argument("--run-id")
    run.add_argument("--jobs", type=int, default=1)
    run.add_argument("--floors", type=Path, default=DEFAULT_FLOORS)
    pin = commands.add_parser(
        "pin-floors", help="Pin semantic regression floors from a finished run report"
    )
    pin.add_argument("--from", dest="source_run", type=Path, required=True)
    pin.add_argument("--floors", type=Path, default=DEFAULT_FLOORS)
    pin.add_argument("--guard", type=float, default=0.002)
    return root


def main(arguments: list[str] | None = None) -> int:
    args = parser().parse_args(arguments)
    try:
        if args.command == "pin-floors":
            return pin_floors(args)
        manifest = load_manifest(args.manifest)
        papers = select_papers(manifest, set(args.id), set(args.tag))
        if args.command in {"fetch", "verify"}:
            for paper in papers:
                for kind, spec in paper["files"].items():
                    path = cached_path(args.cache, paper["id"], kind)
                    state = fetch_file(path, spec, kind) if args.command == "fetch" else "verified"
                    verify_cached_file(path, spec, kind)
                    if kind == "jats":
                        verify_jats_oracle(path, paper["expected"]["jats_elements"])
                    print(f"{state} {paper['id']} {kind} {spec['sha256']}")
            return 0
        if args.jobs < 1 or args.jobs > 4:
            raise CorpusError("CORPUS_RUN_INVALID: --jobs must be between 1 and 4")
        run_id = args.run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise CorpusError("CORPUS_RUN_INVALID: run id must be YYYYMMDDTHHMMSSZ[-slug]")
        run_root = args.runs / run_id
        if run_root.exists():
            raise CorpusError(f"CORPUS_RUN_EXISTS: {run_root}")
        run_root.mkdir(parents=True)
        jats_ids = {paper["id"] for paper in papers if "jats" in paper["files"]}
        floors = load_floors(args.floors) if jats_ids else {}
        if jats_ids:
            missing = jats_ids - set(floors)
            unknown = set(floors) - jats_ids
            if missing:
                raise CorpusError(
                    "CORPUS_FLOORS_MISSING: no pinned floors for " + ", ".join(sorted(missing))
                )
            if unknown:
                raise CorpusError(
                    "CORPUS_FLOORS_MISMATCH: floors cover unknown ids " + ", ".join(sorted(unknown))
                )
        results: list[dict[str, Any]] = []
        if args.jobs == 1:
            for paper in papers:
                print(f"running {paper['id']}", flush=True)
                results.append(run_paper(paper, args.cache, run_root, floors.get(paper["id"])))
        else:
            with ProcessPoolExecutor(max_workers=args.jobs) as executor:
                futures = {
                    executor.submit(
                        run_paper, paper, args.cache, run_root, floors.get(paper["id"])
                    ): paper["id"]
                    for paper in papers
                }
                for future in as_completed(futures):
                    result = future.result()
                    print(f"finished {result['id']} {result['status']}", flush=True)
                    results.append(result)
        report = build_report(manifest, papers, results, run_id)
        (run_root / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (run_root / "report.md").write_text(render_report_markdown(report), encoding="utf-8")
        print(run_root / "report.json")
        return 1 if any(
            result["error"]
            or (result["status_gate"] is not None and not result["status_gate"]["passed"])
            or (
                result["semantic_gate"] is not None
                and not result["semantic_gate"]["passed"]
            )
            for result in results
        ) else 0
    except CorpusError as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
