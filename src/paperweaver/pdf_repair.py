"""Deterministic application of accepted audit proposals to derived views.

This module is the shared core of the audit door's materialization step:
``audit.apply_audit_proposals`` uses it to rebuild ``article.md`` and the
render tree, and ``pdf_contracts.validate_pdf_project`` uses the very same
functions to re-derive the expected view from the immutable base run plus
the proposal ledger. Nothing here performs IO on project state, and no
base-block ledger is ever modified: a repair is a new view over the same
evidence, with every applied proposal recorded in ``transformations``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .pdf_backend import RawPdfObject
from .pdf_table import TableGrid, assign_chars_to_cells, build_payload, verified

PROPOSAL_TYPES = {"table_grid", "equation_latex"}
ISSUE_FOR_TYPE = {
    "table_grid": "PDF_TABLE_UNRESOLVED",
    "equation_latex": "PDF_EQUATION_UNRESOLVED",
}
SUPERSEDABLE_KINDS = {"paragraph", "text", "list", "unknown"}


def page_chars(run_root: Path, pages: set[int]) -> dict[int, list[RawPdfObject]]:
    """Region-agnostic characters for the requested pages, from the raw ledger."""
    result: dict[int, list[RawPdfObject]] = {page: [] for page in pages}
    raw_path = run_root / "raw-objects.jsonl"
    if not raw_path.exists():
        return result
    with raw_path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            raw = json.loads(line)
            page = raw.get("page")
            if raw.get("object_kind") == "char" and page in result:
                result[page].append(
                    RawPdfObject(
                        object_ref=raw["object_ref"],
                        kind=raw["object_kind"],
                        page=raw["page"],
                        bbox=raw["bbox"],
                        payload=raw["payload"],
                        attrs=raw.get("attrs") or {},
                        backend_ref=raw.get("backend_ref", ""),
                    )
                )
    return result


def current_proposals(
    blocks: list[dict[str, Any]], ledger: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Latest accepted proposal per (block, type) that still targets its block.

    A proposal stops being current when the base run no longer carries the
    block as an unresolved target of the proposal's issue (for example after
    a re-import resolves it natively). Order follows first acceptance in the
    append-only ledger, so every consumer derives the same applied set.
    """
    blocks_by_id = {block["block_id"]: block for block in blocks}
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for proposal in ledger:
        if proposal.get("status") != "accepted":
            continue
        latest[(proposal["block_id"], proposal["type"])] = proposal
    selected: list[dict[str, Any]] = []
    for (block_id, proposal_type), proposal in latest.items():
        block = blocks_by_id.get(block_id)
        if block is None or block.get("status") != "unresolved":
            continue
        if ISSUE_FOR_TYPE.get(proposal_type) not in (block.get("issues") or []):
            continue
        selected.append(proposal)
    return selected


def apply_proposals(
    blocks: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
    region_chars: dict[str, list[RawPdfObject]],
    policy: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return the repaired view of ``blocks``; base dicts are never mutated.

    Table proposals contribute grid geometry only: cells are rebuilt here
    from the PDF's own characters and cross-checked against the validation
    recorded at proposal time, so a tampered ledger cannot inject content.
    Body paragraphs whose bbox lies inside the repaired grid are superseded
    by the table (their characters are its cells) and leave the view with an
    explicit ``audit_apply_superseded`` transformation. Equation proposals
    may only promote LaTeX the engine itself did not verify; the payload
    carries ``latex_source: "audited"`` to keep engine verification
    semantics pure.
    """
    by_block = {proposal["block_id"]: proposal for proposal in proposals}
    superseded = _superseded_paragraphs(blocks, proposals, policy)
    applied: list[dict[str, Any]] = []
    for block in blocks:
        block_id = block["block_id"]
        proposal = by_block.get(block_id)
        if proposal is not None:
            if proposal["type"] == "table_grid":
                applied.append(
                    _apply_table(
                        block, proposal, region_chars.get(block_id, []), policy
                    )
                )
            else:
                applied.append(_apply_equation(block, proposal))
            continue
        if block_id in superseded:
            applied.append(
                {
                    **block,
                    "disposition": "excluded_artifact",
                    "transformations": [
                        *block.get("transformations", []),
                        {
                            "type": "audit_apply_superseded",
                            "proposal_id": superseded[block_id],
                        },
                    ],
                }
            )
            continue
        applied.append(block)
    return applied


def _superseded_paragraphs(
    blocks: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
    policy: dict[str, Any],
) -> dict[str, str]:
    """Body paragraphs fully inside a repaired grid: the table owns their text."""
    tolerance = float(policy["table_cell_overlap_tolerance_pt"])
    superseded: dict[str, str] = {}
    for proposal in proposals:
        if proposal["type"] != "table_grid":
            continue
        grid = proposal["payload"]["grid"]
        x0, x1 = grid["x_bounds"][0], grid["x_bounds"][-1]
        y0, y1 = grid["y_bounds"][0], grid["y_bounds"][-1]
        for block in blocks:
            if block["block_id"] == proposal["block_id"]:
                continue
            if block.get("kind") not in SUPERSEDABLE_KINDS:
                continue
            if block.get("disposition") != "render":
                continue
            bbox = block["provenance"][0]["bbox"]
            if (
                x0 - tolerance <= bbox[0]
                and bbox[2] <= x1 + tolerance
                and y0 - tolerance <= bbox[1]
                and bbox[3] <= y1 + tolerance
            ):
                superseded[block["block_id"]] = proposal["proposal_id"]
    return superseded


def applied_payload_digest(proposals: list[dict[str, Any]]) -> str:
    """Content digest over the applied proposal set; the ledger may keep growing."""
    from .pdf_contracts import sha256_bytes
    from .storage import canonical_json

    payload = [
        {
            "proposal_id": item["proposal_id"],
            "block_id": item["block_id"],
            "type": item["type"],
            "payload": item["payload"],
        }
        for item in proposals
    ]
    return sha256_bytes(canonical_json(payload).encode("utf-8"))


REPAIRS_KEYS = {"applied_proposal_ids", "applied_payload_sha256", "applied_at"}


def pinned_proposals(
    root: Path, manifest: dict[str, Any], blocks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The proposal set the manifest pins as applied, verified against the ledger.

    The manifest pins an applied SET, not the whole ledger: new attempts may be
    appended freely, but every pinned proposal must still exist byte-identically
    (ids plus payload digest), otherwise the applied view is unverifiable.
    """
    repairs = manifest.get("repairs")
    if not repairs:
        return []
    if set(repairs) != REPAIRS_KEYS or not isinstance(repairs["applied_proposal_ids"], list):
        raise ValueError("PDF_REPAIR_MANIFEST_INVALID: repairs section is malformed")
    ledger_path = root / "state" / "audit-proposals.jsonl"
    if not ledger_path.is_file():
        raise ValueError("PDF_REPAIR_LEDGER_MISMATCH: repair ledger is missing")
    ledger = [
        json.loads(line)
        for line in ledger_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if any(not isinstance(item, dict) or "proposal_id" not in item for item in ledger):
        raise ValueError(
            "PDF_REPAIR_LEDGER_MISMATCH: repair ledger contains malformed records"
        )
    by_id = {item["proposal_id"]: item for item in ledger}
    missing = [
        proposal_id
        for proposal_id in repairs["applied_proposal_ids"]
        if proposal_id not in by_id
    ]
    if missing:
        raise ValueError(
            "PDF_REPAIR_LEDGER_MISMATCH: applied proposals missing from the ledger"
        )
    pinned = [by_id[proposal_id] for proposal_id in repairs["applied_proposal_ids"]]
    if applied_payload_digest(pinned) != repairs["applied_payload_sha256"]:
        raise ValueError(
            "PDF_REPAIR_LEDGER_MISMATCH: an applied proposal payload changed in the ledger"
        )
    stale = [
        proposal
        for proposal in pinned
        if not _still_targets(blocks, proposal)
    ]
    if stale:
        raise ValueError(
            "PDF_REPAIR_LEDGER_MISMATCH: a pinned proposal no longer matches the base run"
        )
    return pinned


def _still_targets(blocks: list[dict[str, Any]], proposal: dict[str, Any]) -> bool:
    block = next((item for item in blocks if item["block_id"] == proposal["block_id"]), None)
    return (
        block is not None
        and block.get("status") == "unresolved"
        and ISSUE_FOR_TYPE.get(proposal["type"]) in (block.get("issues") or [])
    )


def applied_view(root: Path) -> list[dict[str, Any]]:
    """Load the derived block view: the base run plus current accepted proposals.

    Consumers that materialize user-facing views (translation export) must
    read blocks through this loader instead of the base ledger so a repaired
    table or equation is what ships. Raises ``PDF_REPAIR_LEDGER_MISMATCH``
    when the manifest's applied set no longer matches the ledger.
    """
    from .pdf_contracts import load_policy
    from .pdf_layout import chars_in_bbox

    manifest = json.loads((root / "source" / "pdf" / "manifest.json").read_text(encoding="utf-8"))
    run_root = root / "source" / "pdf" / "runs" / manifest["active_run_id"]
    blocks = [
        json.loads(line)
        for line in (run_root / "base-blocks.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    current = pinned_proposals(root, manifest, blocks)
    if not current:
        return blocks
    policy, _ = load_policy(root / "source" / "pdf" / "policy.json")
    blocks_by_id = {block["block_id"]: block for block in blocks}
    pages = {
        blocks_by_id[item["block_id"]]["provenance"][0]["page"] for item in current
    }
    chars_by_page = page_chars(run_root, pages)
    region_chars = {
        item["block_id"]: chars_in_bbox(
            chars_by_page[blocks_by_id[item["block_id"]]["provenance"][0]["page"]],
            blocks_by_id[item["block_id"]]["provenance"][0]["bbox"],
            policy,
        )
        for item in current
    }
    return apply_proposals(blocks, current, region_chars, policy)


def repair_status(applied_blocks: list[dict[str, Any]], base_status: str) -> str:
    """Derive the delivery status of the applied view from the base status.

    Only an ``incomplete`` base can rise, and only to ``complete_with_repair``
    when the applied view has no unresolved block left. ``unsupported`` and
    ``fatal`` imports are never promoted, and a complete base keeps its own
    status.
    """
    if base_status != "incomplete":
        return base_status
    if any(block.get("status") == "unresolved" for block in applied_blocks):
        return "incomplete"
    return "complete_with_repair"


def _apply_table(
    block: dict[str, Any],
    proposal: dict[str, Any],
    chars: list[RawPdfObject],
    policy: dict[str, Any],
) -> dict[str, Any]:
    grid_data = proposal["payload"]["grid"]
    header_rows = proposal["payload"]["header_rows"]
    grid = TableGrid(
        tuple(float(value) for value in grid_data["x_bounds"]),
        tuple(float(value) for value in grid_data["y_bounds"]),
        header_rows,
    )
    assignment = assign_chars_to_cells(chars, grid, policy)
    validation = proposal.get("validation") or {}
    expected = {
        "coverage": validation.get("coverage"),
        "outside": validation.get("outside"),
        "rows": validation.get("rows"),
        "columns": validation.get("columns"),
    }
    recomputed = {
        "coverage": round(assignment.coverage, 6),
        "outside": assignment.outside,
        "rows": grid.rows,
        "columns": grid.columns,
    }
    if expected != recomputed or not verified(grid, assignment, policy):
        raise ValueError(
            "PDF_REPAIR_EVIDENCE_MISMATCH: "
            f"proposal {proposal['proposal_id']} no longer matches its recorded "
            f"validation ({expected} != {recomputed})"
        )
    payload = build_payload(grid, assignment, style="audited")
    return {
        **block,
        "status": "ok",
        "disposition": "render",
        "table": payload,
        "issues": [code for code in block.get("issues", []) if code != ISSUE_FOR_TYPE["table_grid"]],
        "transformations": [
            *block.get("transformations", []),
            _transformation(proposal),
        ],
    }


def _apply_equation(block: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any]:
    latex = proposal["payload"]["latex"]
    if not isinstance(latex, str) or not latex.strip():
        raise ValueError(
            f"PDF_REPAIR_EVIDENCE_MISMATCH: proposal {proposal['proposal_id']} lost its LaTeX"
        )
    base_payload = block.get("equation") or {}
    payload = {
        **base_payload,
        "latex": latex,
        "latex_verified": False,
        "latex_source": "audited",
    }
    return {
        **block,
        "kind": "equation",
        "status": "ok",
        "disposition": "render",
        "equation": payload,
        "issues": [
            code
            for code in block.get("issues", [])
            if code != ISSUE_FOR_TYPE["equation_latex"]
        ],
        "transformations": [
            *block.get("transformations", []),
            _transformation(proposal),
        ],
    }


def _transformation(proposal: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "audit_apply",
        "proposal_id": proposal["proposal_id"],
        "model": proposal["model"],
        "revision": proposal["revision"],
        "created_at": proposal["created_at"],
    }
