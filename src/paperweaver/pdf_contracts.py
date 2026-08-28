"""Stable identities and strict invariants for PDF-derived records."""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from pathlib import Path
from typing import Any

from .storage import canonical_json

SCHEMA_VERSION = 1
ADAPTER_SCHEMA_VERSION = "paperweaver-pdf-v1"
COMPLETE_STATUSES = {"complete", "complete_with_warnings", "complete_with_repair"}
ALL_STATUSES = {"fatal", "unsupported", "incomplete", *COMPLETE_STATUSES}


class PdfUnsupportedError(ValueError):
    """A valid PDF exceeds the active deterministic import policy."""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def stable_id(prefix: str, *parts: object) -> str:
    serialised = "\x1f".join(str(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(serialised.encode()).hexdigest()[:16]}"


def make_run_id(
    source_sha256: str,
    backend_name: str,
    backend_version: str,
    backend_options: dict[str, Any],
    policy_sha256: str,
) -> str:
    return stable_id(
        "run",
        source_sha256,
        backend_name,
        backend_version,
        canonical_json(backend_options),
        policy_sha256,
        ADAPTER_SCHEMA_VERSION,
    )


def make_object_ref(
    page: int,
    kind: str,
    bbox: list[float],
    payload: str,
    occurrence: int,
) -> str:
    quantised = ",".join(f"{coordinate:.3f}" for coordinate in bbox)
    return stable_id("obj", page, kind, quantised, sha256_bytes(payload.encode()), occurrence)


def make_block_id(source_sha256: str, run_id: str, object_refs: list[str]) -> str:
    return stable_id("blk", source_sha256, run_id, *sorted(object_refs))


def default_policy() -> dict[str, Any]:
    resource = files("paperweaver").joinpath("policies/pdf-born-digital-v1.json")
    return json.loads(resource.read_text(encoding="utf-8"))


def load_policy(path: Path | None = None) -> tuple[dict[str, Any], str]:
    policy = json.loads(path.read_text(encoding="utf-8")) if path else default_policy()
    _validate_policy(policy)
    digest = sha256_bytes(canonical_json(policy).encode())
    return policy, digest


def validate_bbox(bbox: list[float], width: float, height: float) -> None:
    if len(bbox) != 4:
        raise ValueError("PDF_BBOX_INVALID: bbox must contain four coordinates")
    x0, y0, x1, y1 = bbox
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise ValueError(
            f"PDF_BBOX_INVALID: {bbox} is outside page bounds [0, 0, {width}, {height}]"
        )


def pdf_status(root: Path) -> str:
    return validate_pdf_project(root, require_complete=False)


def validate_pdf_project(root: Path, *, require_complete: bool) -> str:
    source_root = root / "source"
    path = root / "source" / "pdf" / "manifest.json"
    if not path.exists():
        raise FileNotFoundError(f"Not a PDF PaperWeaver project: {root}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    status = manifest["status"]
    if status not in ALL_STATUSES:
        raise ValueError(f"PDF_MANIFEST_INVALID: unknown status {status!r}")
    if status == "complete_with_repair" and not isinstance(manifest.get("repairs"), dict):
        raise RuntimeError(
            "PDF_REPAIR_MANIFEST_INVALID: complete_with_repair requires applied repairs"
        )
    source = json.loads((source_root / "source.json").read_text(encoding="utf-8"))
    original_digest = sha256_bytes((source_root / "original.pdf").read_bytes())
    if not (
        source.get("format") == "pdf"
        and source.get("sha256") == manifest["source_sha256"] == original_digest
    ):
        raise RuntimeError("PDF_SOURCE_DIGEST_MISMATCH: original PDF identity changed")
    article = (source_root / "article.md").read_bytes()
    if sha256_bytes(article) != manifest["article_sha256"]:
        raise RuntimeError("PDF_ARTICLE_DIGEST_MISMATCH: article.md is not the materialized view")
    policy = json.loads((source_root / "pdf" / "policy.json").read_text(encoding="utf-8"))
    _validate_policy(policy)
    if sha256_bytes(canonical_json(policy).encode()) != manifest["policy"]["sha256"]:
        raise RuntimeError("PDF_POLICY_DIGEST_MISMATCH: active PDF policy changed")
    qa = json.loads((source_root / "pdf" / "qa.json").read_text(encoding="utf-8"))
    if not (
        qa["status"] == status
        and qa["source_sha256"] == manifest["source_sha256"]
        and qa["run_id"] == manifest["active_run_id"]
        and qa["materialization_id"] == manifest["materialization_id"]
    ):
        raise RuntimeError("PDF_QA_MANIFEST_MISMATCH: QA and manifest disagree")
    run_root = source_root / "pdf" / "runs" / manifest["active_run_id"]
    _validate_artifact_digests(source_root, manifest)
    blocks = _read_jsonl(run_root / "base-blocks.jsonl")
    raw = _read_jsonl(run_root / "raw-objects.jsonl")
    relations = _read_jsonl(run_root / "base-relations.jsonl")
    accounting = _read_jsonl(run_root / "object-accounting.jsonl")
    raw_refs = [item["object_ref"] for item in raw]
    accounted_refs = [item["object_ref"] for item in accounting]
    if (
        len(raw_refs) != len(set(raw_refs))
        or len(accounted_refs) != len(set(accounted_refs))
        or set(raw_refs) != set(accounted_refs)
    ):
        raise RuntimeError("PDF_OBJECT_ACCOUNTING_INCOMPLETE: raw object ledger is not total")
    from .models import PdfBlock, PdfObjectAccounting, PdfRelation

    try:
        block_records = [PdfBlock(**item) for item in blocks]
        accounting_records = [PdfObjectAccounting(**item) for item in accounting]
        relation_records = [PdfRelation(**item) for item in relations]
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"PDF_LEDGER_SCHEMA_INVALID: {error}") from error
    raw_by_ref = {item["object_ref"]: item for item in raw}
    block_by_id = {item.block_id: item for item in block_records}
    block_ids = set(block_by_id)
    if (
        len(block_ids) != len(block_records)
        or [item.ordinal for item in block_records] != list(range(1, len(block_records) + 1))
    ):
        raise RuntimeError("PDF_BLOCK_LEDGER_INVALID: block ordinals or identities are not unique")
    for block in block_records:
        if (
            block.source_sha256 != manifest["source_sha256"]
            or block.run_id != manifest["active_run_id"]
            or not block.source_object_refs
            or len(block.source_object_refs) != len(set(block.source_object_refs))
            or not set(block.source_object_refs) <= set(raw_refs)
            or block.block_id
            != make_block_id(
                manifest["source_sha256"],
                manifest["active_run_id"],
                block.source_object_refs,
            )
        ):
            raise RuntimeError("PDF_BLOCK_LEDGER_INVALID: block identity or source refs are invalid")
        for provenance in block.provenance:
            if provenance.get("page", 0) < 1:
                raise RuntimeError("PDF_BBOX_INVALID: provenance page must be positive")
            try:
                validate_bbox(
                    provenance["bbox"],
                    float(provenance["page_width"]),
                    float(provenance["page_height"]),
                )
            except (KeyError, TypeError, ValueError) as error:
                raise RuntimeError(f"PDF_BBOX_INVALID: {error}") from error
    expected_materialization_id = stable_id(
        "mat",
        manifest["active_run_id"],
        sha256_bytes(canonical_json(blocks).encode()),
    )
    if manifest["materialization_id"] != expected_materialization_id:
        raise RuntimeError(
            "PDF_MATERIALIZATION_ID_MISMATCH: blocks do not match the materialization identity"
        )
    for block in blocks:
        if block["kind"] != "equation":
            continue
        payload = block.get("equation")
        if not payload or not block.get("asset_refs"):
            raise RuntimeError("PDF_EQUATION_INVALID: equation payload or crop is missing")
        verified = bool(payload.get("latex_verified") and payload.get("latex"))
        if (block["status"] == "ok") != verified:
            raise RuntimeError("PDF_EQUATION_INVALID: status and verified LaTeX disagree")
    for item in accounting_records:
        raw_item = raw_by_ref[item.object_ref]
        if item.object_kind != raw_item["object_kind"]:
            raise RuntimeError("PDF_OBJECT_ACCOUNTING_INVALID: object kinds disagree")
        if not set(item.supporting_block_ids) <= block_ids:
            raise RuntimeError("PDF_OBJECT_ACCOUNTING_INVALID: supporting block is missing")
        if item.primary_disposition in {"rendered", "unresolved"}:
            if (
                item.primary_block_id not in block_ids
                or item.duplicate_of is not None
                or item.object_ref
                not in block_by_id[item.primary_block_id].source_object_refs
            ):
                raise RuntimeError("PDF_OBJECT_ACCOUNTING_INVALID: primary owner is invalid")
        elif item.primary_disposition == "excluded_artifact":
            if item.primary_block_id is not None or not item.reason_code:
                raise RuntimeError("PDF_OBJECT_ACCOUNTING_INVALID: artifact reason is missing")
        elif item.primary_disposition == "duplicate" and (
            item.primary_block_id is not None
            or item.duplicate_of not in raw_by_ref
            or item.duplicate_of == item.object_ref
        ):
            raise RuntimeError("PDF_OBJECT_ACCOUNTING_INVALID: duplicate target is invalid")
    relation_ids: set[str] = set()
    for relation in relation_records:
        expected_relation_id = stable_id(
            "rel",
            relation.type,
            *relation.from_block_ids,
            *relation.to_block_ids,
        )
        if (
            relation.relation_id in relation_ids
            or relation.relation_id != expected_relation_id
            or relation.type not in {
                "caption_of",
                "note_of",
                "number_of",
                "callout_to",
                "continues",
                "contains",
                "reading_before",
                "duplicate_of",
                "derived_from",
            }
            or not relation.from_block_ids
            or not relation.to_block_ids
            or not set(relation.from_block_ids + relation.to_block_ids) <= block_ids
        ):
            raise RuntimeError("PDF_RELATION_LEDGER_INVALID: relation is not canonical")
        relation_ids.add(relation.relation_id)
    assets = _read_jsonl(source_root / "assets" / "manifest.jsonl")
    asset_paths: dict[str, str] = {}
    asset_ids: set[str] = set()
    for asset in assets:
        relative = Path(asset["path"])
        if relative.is_absolute() or ".." in relative.parts or relative.parts[:1] != ("source",):
            raise RuntimeError("PDF_ASSET_PATH_INVALID: asset path escapes source workspace")
        asset_path = root / relative
        if sha256_bytes(asset_path.read_bytes()) != asset["sha256"]:
            raise RuntimeError("PDF_ASSET_DIGEST_MISMATCH: derived visual evidence changed")
        if asset["asset_id"] in asset_ids:
            raise RuntimeError("PDF_ASSET_MANIFEST_INVALID: duplicate asset id")
        asset_ids.add(asset["asset_id"])
        asset_paths[asset["asset_id"]] = Path(*relative.parts[1:]).as_posix()
    if any(not set(block.asset_refs) <= asset_ids for block in block_records):
        raise RuntimeError("PDF_ASSET_MANIFEST_INVALID: block asset reference is missing")
    unresolved_view: list[dict[str, Any]] = blocks
    view_records = block_records
    repairs_section = manifest.get("repairs")
    if repairs_section is not None:
        from .pdf_repair import apply_proposals, pinned_proposals

        try:
            current = pinned_proposals(root, manifest, blocks)
        except ValueError as error:
            raise RuntimeError(str(error)) from error
        applied = apply_proposals(
            blocks,
            current,
            _repair_region_chars(run_root, blocks, current, policy),
            policy,
        )
        try:
            view_records = [PdfBlock(**item) for item in applied]
        except (TypeError, ValueError) as error:
            raise RuntimeError(f"PDF_LEDGER_SCHEMA_INVALID: {error}") from error
        for item in applied:
            if item["kind"] != "equation" or item["status"] != "ok":
                continue
            payload = item.get("equation") or {}
            if not (
                payload.get("latex")
                and (payload.get("latex_verified") or payload.get("latex_source") == "audited")
            ):
                raise RuntimeError(
                    "PDF_EQUATION_INVALID: audited equation payload lost its evidence"
                )
        unresolved_view = applied
    from .pdf_markdown import materialize_markdown

    generated, generated_map, generated_tree = materialize_markdown(
        view_records, manifest["materialization_id"], asset_paths
    )
    if generated.encode() != article:
        raise RuntimeError("PDF_ARTICLE_MATERIALIZATION_MISMATCH: article is not derived from blocks")
    stored_map = _read_jsonl(source_root / "article-map.jsonl")
    if stored_map != [item.to_dict() for item in generated_map]:
        raise RuntimeError("PDF_ARTICLE_MAP_MISMATCH: Markdown provenance map changed")
    stored_tree = json.loads((source_root / "pdf" / "render-tree.json").read_text(encoding="utf-8"))
    if stored_tree != generated_tree:
        raise RuntimeError("PDF_RENDER_TREE_MISMATCH: render tree changed")
    if status in COMPLETE_STATUSES and any(
        item["status"] == "unresolved" for item in unresolved_view
    ):
        raise RuntimeError("PDF_GATE_INVALID: complete manifest contains unresolved blocks")
    if require_complete and status not in COMPLETE_STATUSES:
        raise RuntimeError(
            f"PDF_IMPORT_INCOMPLETE: PDF status is {status}; review source/pdf/qa.md"
        )
    return status


ARTIFACT_BASE_PATHS = {
    "backend": "pdf/runs/{run_id}/backend.json",
    "raw_objects": "pdf/runs/{run_id}/raw-objects.jsonl",
    "base_blocks": "pdf/runs/{run_id}/base-blocks.jsonl",
    "base_relations": "pdf/runs/{run_id}/base-relations.jsonl",
    "object_accounting": "pdf/runs/{run_id}/object-accounting.jsonl",
    "asset_manifest": "assets/manifest.jsonl",
    "article_map": "article-map.jsonl",
    "render_tree": "pdf/render-tree.json",
    "qa": "pdf/qa.json",
}


def artifact_paths(run_id: str) -> dict[str, str]:
    """Canonical source-relative paths of the digest-tracked PDF artifacts."""
    return {name: template.format(run_id=run_id) for name, template in ARTIFACT_BASE_PATHS.items()}


def _validate_artifact_digests(source_root: Path, manifest: dict[str, Any]) -> None:
    expected = artifact_paths(manifest["active_run_id"])
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != set(expected):
        raise RuntimeError("PDF_ARTIFACT_MANIFEST_INVALID: artifact digest inventory is incomplete")
    for name, expected_path in expected.items():
        item = artifacts[name]
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise RuntimeError("PDF_ARTIFACT_MANIFEST_INVALID: artifact entry is malformed")
        relative = Path(item["path"])
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or relative.as_posix() != expected_path
        ):
            raise RuntimeError("PDF_ARTIFACT_PATH_INVALID: artifact path is not canonical")
        artifact = source_root / relative
        if not artifact.is_file() or sha256_bytes(artifact.read_bytes()) != item["sha256"]:
            raise RuntimeError(f"PDF_ARTIFACT_DIGEST_MISMATCH: {name} changed")


def _repair_region_chars(
    run_root: Path,
    blocks: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
    policy: dict[str, Any],
) -> dict[str, list[Any]]:
    from .pdf_layout import chars_in_bbox
    from .pdf_repair import page_chars

    blocks_by_id = {block["block_id"]: block for block in blocks}
    pages = {blocks_by_id[item["block_id"]]["provenance"][0]["page"] for item in proposals}
    chars_by_page = page_chars(run_root, pages)
    return {
        item["block_id"]: chars_in_bbox(
            chars_by_page[blocks_by_id[item["block_id"]]["provenance"][0]["page"]],
            blocks_by_id[item["block_id"]]["provenance"][0]["bbox"],
            policy,
        )
        for item in proposals
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise RuntimeError(f"PDF_LEDGER_MISSING: {path.relative_to(path.parents[3])}")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _validate_policy(policy: dict[str, Any]) -> None:
    expected = {
        "schema_version",
        "name",
        "max_file_bytes",
        "max_pages",
        "max_leaf_objects",
        "max_rendered_page_pixels",
        "min_usable_text_page_ratio",
        "min_valid_unicode_ratio",
        "max_replacement_character_warning_ratio",
        "max_replacement_character_ratio",
        "max_rotated_body_character_ratio",
        "min_visible_ink_accounting_ratio",
        "min_one_or_two_column_page_ratio",
        "render_scale",
        "ink_luminance_threshold",
        "header_footer_zone_ratio",
        "repeated_artifact_page_ratio",
        "min_repeated_artifact_pages",
        "line_y_tolerance_points",
        "line_split_gap_points",
        "column_min_shared_lines",
        "column_gap_ratio",
        "rule_thickness_pt",
        "table_max_rule_span_pt",
        "table_region_gap_pt",
        "table_edge_tolerance_pt",
        "table_cell_overlap_tolerance_pt",
        "table_light_min_rule_width_pt",
        "table_light_min_row_rule_ratio",
        "table_light_column_gap_factor",
        "table_light_max_row_gap_pt",
        "min_table_char_coverage",
    }
    if set(policy) != expected:
        raise ValueError(
            "PDF_POLICY_INVALID: expected exactly " + ", ".join(sorted(expected))
        )
    if policy["schema_version"] != 1 or policy["name"] != "born-digital-journal-v1":
        raise ValueError("PDF_POLICY_INVALID: unsupported policy identity")
    ratios = [name for name in expected if name.endswith("_ratio")]
    ratios.append("min_table_char_coverage")
    if any(not 0 <= policy[name] <= 1 for name in ratios):
        raise ValueError("PDF_POLICY_INVALID: ratios must be between zero and one")
    limits = {
        "max_file_bytes": (1, 536_870_912),
        "max_pages": (1, 1000),
        "max_leaf_objects": (1, 2_000_000),
        "max_rendered_page_pixels": (1, 100_000_000),
        "render_scale": (0.5, 4.0),
        "ink_luminance_threshold": (1, 254),
        "line_y_tolerance_points": (0.1, 10.0),
        "line_split_gap_points": (1.0, 200.0),
        "column_min_shared_lines": (1, 100),
        "min_repeated_artifact_pages": (1, 100),
        "table_region_gap_pt": (1.0, 1000.0),
        "table_edge_tolerance_pt": (0.1, 50.0),
        "table_cell_overlap_tolerance_pt": (0.1, 50.0),
        "table_light_min_rule_width_pt": (1.0, 500.0),
        "table_light_column_gap_factor": (0.05, 5.0),
        "table_light_max_row_gap_pt": (5.0, 400.0),
        "table_max_rule_span_pt": (50.0, 2000.0),
    }
    for name, (minimum, maximum) in limits.items():
        if not minimum <= policy[name] <= maximum:
            raise ValueError(
                f"PDF_POLICY_INVALID: {name} must be between {minimum} and {maximum}"
            )
    baseline = default_policy()
    maximum_limits = {
        "max_file_bytes",
        "max_pages",
        "max_leaf_objects",
        "max_rendered_page_pixels",
        "max_replacement_character_warning_ratio",
        "max_replacement_character_ratio",
        "max_rotated_body_character_ratio",
    }
    minimum_limits = {
        "min_usable_text_page_ratio",
        "min_valid_unicode_ratio",
        "min_visible_ink_accounting_ratio",
        "min_one_or_two_column_page_ratio",
        "min_table_char_coverage",
        "render_scale",
        "ink_luminance_threshold",
    }
    if any(policy[name] > baseline[name] for name in maximum_limits) or any(
        policy[name] < baseline[name] for name in minimum_limits
    ):
        raise ValueError("PDF_POLICY_INVALID: custom policy weakens a hard invariant")
    if not (
        0
        <= policy["max_replacement_character_warning_ratio"]
        <= policy["max_replacement_character_ratio"]
    ):
        raise ValueError("PDF_POLICY_INVALID: replacement warning threshold is invalid")
