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
EXPECTED_KEYS = {"jats_elements", "approved_differences"}
JATS_ELEMENT_KEYS = {"figures", "tables", "equations", "references"}


class CorpusError(RuntimeError):
    """A stable, user-facing corpus bootstrap or run error."""


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CorpusError(f"CORPUS_MANIFEST_INVALID: {error}") from error
    _require_exact_keys(manifest, ROOT_KEYS, "manifest")
    if manifest["schema_version"] != 1:
        raise CorpusError("CORPUS_MANIFEST_INVALID: schema_version must be 1")
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
    return {
        "method": "unicode-token-multiset-diagnostic-v1",
        "reference_tokens": len(reference),
        "candidate_tokens": len(candidate),
        "overlap_tokens": overlap,
        "recall": overlap / len(reference) if reference else 1.0,
        "precision": overlap / len(candidate) if candidate else 1.0,
        "gating": False,
    }


def _is_cjk(character: str) -> bool:
    value = ord(character)
    return (
        0x3400 <= value <= 0x4DBF
        or 0x4E00 <= value <= 0x9FFF
        or 0xF900 <= value <= 0xFAFF
        or 0x20000 <= value <= 0x3134F
    )


def _jats_body_text(path: Path) -> str:
    root = ET.parse(path).getroot()
    body = next((item for item in root.iter() if item.tag.rsplit("}", 1)[-1] == "body"), None)
    return " ".join(body.itertext()) if body is not None else ""


def _article_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    text = re.sub(r"!\[[^]]*]\([^)]*\)", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[#|`*_\\$]+", " ", text)


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


def run_paper(paper: dict[str, Any], cache_root: Path, run_root: Path) -> dict[str, Any]:
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
        result["metrics"] = qa["metrics"]
        result["issue_codes"] = dict(sorted(Counter(item["code"] for item in qa["issues"]).items()))
        if "jats" in paper["files"]:
            result["token_diagnostics"] = token_diagnostics(
                diagnostic_tokens(_jats_body_text(jats)),
                diagnostic_tokens(_article_text(project / "source" / "article.md")),
            )
    except (CorpusError, FileExistsError, FileNotFoundError, PdfUnsupportedError, RuntimeError, ValueError) as error:
        result["error"] = f"{type(error).__name__}: {error}"
    result["duration_seconds"] = round(time.monotonic() - started, 3)
    return result


def build_report(
    manifest: dict[str, Any], papers: list[dict[str, Any]], results: list[dict[str, Any]], run_id: str
) -> dict[str, Any]:
    statuses = Counter(result["status"] for result in results)
    return {
        "schema_version": 1,
        "corpus_id": manifest["corpus_id"],
        "run_id": run_id,
        "environment": environment_record(),
        "selection": [paper["id"] for paper in papers],
        "summary": {"papers": len(results), "statuses": dict(sorted(statuses.items()))},
        "results": sorted(results, key=lambda item: item["id"]),
    }


def render_report_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Open PDF corpus report",
        "",
        f"- Corpus: `{report['corpus_id']}`",
        f"- Run: `{report['run_id']}`",
        f"- Papers: `{report['summary']['papers']}`",
        f"- Statuses: `{json.dumps(report['summary']['statuses'], sort_keys=True)}`",
        "",
        "| Paper | Status | Pages | Figures | Tables | Equations | Unresolved | Seconds |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in report["results"]:
        metrics = item["metrics"]
        lines.append(
            f"| `{item['id']}` | {item['status']} | {metrics.get('pages', '')} | "
            f"{metrics.get('verified_figures', '')} | {metrics.get('verified_tables', '')} | "
            f"{metrics.get('verified_equations', '')} | {metrics.get('unresolved_blocks', '')} | "
            f"{item['duration_seconds']} |"
        )
        if item["error"]:
            lines.append(f"\nError for `{item['id']}`: `{item['error']}`\n")
    return "\n".join(lines) + "\n"


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
    return root


def main(arguments: list[str] | None = None) -> int:
    args = parser().parse_args(arguments)
    try:
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
        results: list[dict[str, Any]] = []
        if args.jobs == 1:
            for paper in papers:
                print(f"running {paper['id']}", flush=True)
                results.append(run_paper(paper, args.cache, run_root))
        else:
            with ProcessPoolExecutor(max_workers=args.jobs) as executor:
                futures = {
                    executor.submit(run_paper, paper, args.cache, run_root): paper["id"]
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
        return 1 if any(result["error"] for result in results) else 0
    except CorpusError as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
