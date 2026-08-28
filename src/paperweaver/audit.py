"""The audit door: work orders out, validated repair proposals in.

Work orders describe unresolved blocks with their evidence (bbox, crop,
caption, glyph inventory). Proposals never carry content text: a table
proposal contributes only grid geometry whose cells the engine rebuilds
from the PDF's own characters, and an equation proposal must have every
region glyph consumed by its LaTeX. Every submission is appended to the
state ledger with its validation outcome; nothing here edits the
immutable PDF run.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import AuditProposal, PdfBlock
from .pdf_backend import RawPdfObject
from .pdf_contracts import (
    artifact_paths,
    load_policy,
    sha256_bytes,
    stable_id,
    validate_pdf_project,
)
from .pdf_equation import latex_balanced, latex_char
from .pdf_layout import chars_in_bbox
from .pdf_markdown import materialize_markdown
from .pdf_qa import render_qa_markdown
from .pdf_repair import (
    ISSUE_FOR_TYPE,
    applied_payload_digest,
    apply_proposals,
    current_proposals,
    page_chars,
    repair_status,
)
from .pdf_table import TableGrid, assign_chars_to_cells, verified
from .storage import (
    append_jsonl,
    atomic_write_json,
    atomic_write_text,
    read_jsonl,
    write_dict_jsonl,
)

SCHEMA_VERSION = 1
PROPOSAL_TYPES = {"table_grid", "equation_latex"}
TARGET_ISSUE_TYPES = {issue: kind for kind, issue in ISSUE_FOR_TYPE.items()}
TABLE_KEYS = {"work_order_id", "type", "grid", "header_rows"}
EQUATION_KEYS = {"work_order_id", "type", "latex"}


def export_audit_package(root: Path) -> Path:
    """Write output/audit-package.json describing every repairable block."""
    state = _load_pdf_state(root)
    ledger = _load_proposals(root)
    work_orders = _work_orders(state, ledger)
    package = {
        "schema_version": SCHEMA_VERSION,
        "source_sha256": state.source_sha256,
        "run_id": state.run_id,
        "policy_sha256": state.policy_sha256,
        "exported_at": _now(),
        "work_orders": work_orders,
    }
    output = root / "output" / "audit-package.json"
    atomic_write_json(output, package)
    return output


def import_audit_proposals(root: Path, draft: Path, adapter: str, model: str) -> tuple[int, int]:
    """Validate every draft line and append its outcome to the state ledger."""
    proposals = _plan_proposals(root, draft, adapter, model)
    for proposal in proposals:
        append_jsonl(root / "state" / "audit-proposals.jsonl", proposal)
    accepted = sum(proposal.status == "accepted" for proposal in proposals)
    return accepted, len(proposals) - accepted


def verify_audit_draft(root: Path, draft: Path) -> list[AuditProposal]:
    """Validate a draft without touching any state; the caller prints verdicts."""
    return _plan_proposals(root, draft, "verify-draft", "dry-run")


def chars_for_block(root: Path, block_id: str) -> list[RawPdfObject]:
    """Region characters for one block; the raw material skills grid against."""
    state = _load_pdf_state(root)
    block = next((item for item in state.blocks if item["block_id"] == block_id), None)
    if block is None:
        raise ValueError(f"AUDIT_INVALID: unknown block {block_id}")
    provenance = block["provenance"][0]
    chars = page_chars(state.run_root, {provenance["page"]})[provenance["page"]]
    return chars_in_bbox(chars, provenance["bbox"], state.policy)


def apply_audit_proposals(root: Path) -> dict[str, Any]:
    """Materialize every current accepted proposal into the derived views.

    Rewrites ``article.md``, ``article-map.jsonl``, ``render-tree.json``,
    ``qa.json``/``qa.md`` and the manifest from the immutable base run plus
    the proposal ledger. The run is idempotent: the same ledger produces
    byte-identical outputs because ``applied_at`` is derived from the
    proposals themselves. Base ledgers are never touched.
    """
    state = _load_pdf_state(root)
    manifest_path = root / "source" / "pdf" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    base_status = manifest["status"]
    if base_status in {"fatal", "unsupported"}:
        raise ValueError(f"AUDIT_APPLY_INVALID: a {base_status} import cannot be repaired")
    validate_pdf_project(root, require_complete=False)
    ledger = _load_proposals(root)
    current = current_proposals(state.blocks, [item.to_dict() for item in ledger])
    if not current:
        raise ValueError(
            "AUDIT_APPLY_NOTHING: no accepted proposal applies to the current base run"
        )
    blocks_by_id = {block["block_id"]: block for block in state.blocks}
    pages = {
        blocks_by_id[item["block_id"]]["provenance"][0]["page"] for item in current
    }
    chars_by_page = page_chars(state.run_root, pages)
    region_chars = {
        item["block_id"]: chars_in_bbox(
            chars_by_page[blocks_by_id[item["block_id"]]["provenance"][0]["page"]],
            blocks_by_id[item["block_id"]]["provenance"][0]["bbox"],
            state.policy,
        )
        for item in current
    }
    applied = apply_proposals(state.blocks, current, region_chars, state.policy)
    applied_records = [PdfBlock(**item) for item in applied]
    asset_paths = {
        asset_id: Path(*Path(asset["path"]).parts[1:]).as_posix()
        for asset_id, asset in state.assets.items()
    }
    markdown, mapping, tree = materialize_markdown(
        applied_records, manifest["materialization_id"], asset_paths
    )
    new_status = repair_status(applied, base_status)
    qa = _repair_qa(root, applied, current, new_status)
    source_root = root / "source"
    atomic_write_text(source_root / "article.md", markdown)
    write_dict_jsonl(source_root / "article-map.jsonl", [item.to_dict() for item in mapping])
    atomic_write_json(source_root / "pdf" / "render-tree.json", tree)
    atomic_write_json(source_root / "pdf" / "qa.json", qa)
    atomic_write_text(source_root / "pdf" / "qa.md", render_qa_markdown(qa))
    manifest["status"] = new_status
    manifest["article_sha256"] = sha256_bytes(markdown.encode())
    manifest["repairs"] = {
        "applied_proposal_ids": [item["proposal_id"] for item in current],
        "applied_payload_sha256": applied_payload_digest(current),
        "applied_at": max(item["created_at"] for item in current),
    }
    manifest["artifacts"] = {
        name: {
            "path": relative,
            "sha256": sha256_bytes((source_root / relative).read_bytes()),
        }
        for name, relative in artifact_paths(manifest["active_run_id"]).items()
    }
    atomic_write_json(manifest_path, manifest)
    tables = sum(item["type"] == "table_grid" for item in current)
    return {
        "status": new_status,
        "applied": len(current),
        "table_grids": tables,
        "equation_latex": len(current) - tables,
    }


def _repair_qa(
    root: Path,
    applied: list[dict[str, Any]],
    current: list[dict[str, Any]],
    new_status: str,
) -> dict[str, Any]:
    """Refresh QA for the applied view: honest metrics plus one audit trail issue."""
    qa = json.loads((root / "source" / "pdf" / "qa.json").read_text(encoding="utf-8"))
    repaired_ids = {item["block_id"] for item in current}
    repaired_codes = {ISSUE_FOR_TYPE[item["type"]] for item in current}
    issues = [
        issue
        for issue in qa["issues"]
        if issue["code"] != "PDF_REPAIR_APPLIED"
        and not (issue["code"] in repaired_codes and set(issue["block_ids"]) & repaired_ids)
    ]
    issues.append(
        {
            "issue_id": stable_id("issue", "PDF_REPAIR_APPLIED", *sorted(repaired_ids)),
            "severity": "warning",
            "code": "PDF_REPAIR_APPLIED",
            "message": (
                f"{len(current)} audited repair proposal(s) applied to the materialized view."
            ),
            "block_ids": sorted(repaired_ids),
            "provenance": [],
            "asset_refs": [],
        }
    )
    qa["issues"] = sorted(
        issues, key=lambda item: (item["severity"], item["code"], item["issue_id"])
    )
    qa["status"] = new_status
    qa["metrics"]["verified_tables"] = sum(
        1
        for block in applied
        if block["kind"] == "table"
        and block["status"] == "ok"
        and block.get("table")
        and block["table"].get("structure_verified")
    )
    qa["metrics"]["verified_equations"] = sum(
        1
        for block in applied
        if block["kind"] == "equation"
        and bool(block.get("equation"))
        and bool(block["equation"].get("latex_verified"))
    )
    qa["metrics"]["unresolved_blocks"] = sum(
        1 for block in applied if block["status"] == "unresolved"
    )
    qa["repairs"] = {
        "tables": sum(item["type"] == "table_grid" for item in current),
        "equations": sum(item["type"] == "equation_latex" for item in current),
        "total": len(current),
    }
    return qa


def audit_status(root: Path) -> dict[str, Any]:
    """Summarize repair burn-down over the current unresolved targets."""
    state = _load_pdf_state(root)
    ledger = _load_proposals(root)
    work_orders = _work_orders(state, ledger)
    accepted_blocks = {
        proposal.block_id
        for proposal in ledger
        if proposal.status == "accepted"
    }
    rejected_blocks = {
        proposal.block_id
        for proposal in ledger
        if proposal.status == "rejected"
    }
    touched = {order["block_id"] for order in work_orders} & (accepted_blocks | rejected_blocks)
    return {
        "source_sha256": state.source_sha256,
        "targets": len(work_orders),
        "accepted": len({order["block_id"] for order in work_orders} & accepted_blocks),
        "attempted_not_accepted": len(touched - accepted_blocks),
        "untouched": len(work_orders) - len(touched),
        "proposals": len(ledger),
        "acceptance_rate": (
            sum(proposal.status == "accepted" for proposal in ledger) / len(ledger)
            if ledger
            else None
        ),
    }


class _PdfState:
    """Everything the audit door needs from one frozen PDF import."""

    def __init__(
        self,
        root: Path,
        source_sha256: str,
        run_id: str,
        run_root: Path,
        policy: dict[str, Any],
        policy_sha256: str,
        blocks: list[dict[str, Any]],
        relations: list[dict[str, Any]],
        assets: dict[str, dict[str, Any]],
    ) -> None:
        self.root = root
        self.source_sha256 = source_sha256
        self.run_id = run_id
        self.run_root = run_root
        self.policy = policy
        self.policy_sha256 = policy_sha256
        self.blocks = blocks
        self.relations = relations
        self.assets = assets


def _load_pdf_state(root: Path) -> _PdfState:
    source = json.loads((root / "source" / "source.json").read_text(encoding="utf-8"))
    if source.get("format") != "pdf":
        raise ValueError("AUDIT_INVALID: project source is not a PDF import")
    manifest = json.loads(
        (root / "source" / "pdf" / "manifest.json").read_text(encoding="utf-8")
    )
    run_root = root / "source" / "pdf" / "runs" / manifest["active_run_id"]
    qa = json.loads((root / "source" / "pdf" / "qa.json").read_text(encoding="utf-8"))
    blocks = [json.loads(line) for line in (run_root / "base-blocks.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    relations_path = run_root / "base-relations.jsonl"
    relations = (
        [json.loads(line) for line in relations_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if relations_path.exists()
        else []
    )
    assets: dict[str, dict[str, Any]] = {}
    assets_path = root / "source" / "assets" / "manifest.jsonl"
    if assets_path.exists():
        for line in assets_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                asset = json.loads(line)
                assets[asset["asset_id"]] = asset
    policy, _ = load_policy(root / "source" / "pdf" / "policy.json")
    return _PdfState(
        root=root,
        source_sha256=manifest["source_sha256"],
        run_id=manifest["active_run_id"],
        run_root=run_root,
        policy=policy,
        policy_sha256=qa["policy"]["sha256"],
        blocks=blocks,
        relations=relations,
        assets=assets,
    )


def _load_proposals(root: Path) -> list[AuditProposal]:
    return read_jsonl(root / "state" / "audit-proposals.jsonl", AuditProposal)


def _work_orders(state: _PdfState, ledger: list[AuditProposal]) -> list[dict[str, Any]]:
    blocks_by_id = {block["block_id"]: block for block in state.blocks}
    captions = _caption_index(state, blocks_by_id)
    targets = []
    for block in state.blocks:
        kind = _target_type(block)
        if kind:
            targets.append((block, kind))
    chars_by_page = page_chars(state.run_root, {block["provenance"][0]["page"] for block, _ in targets})
    attempts: dict[str, list[AuditProposal]] = {}
    for proposal in ledger:
        attempts.setdefault(proposal.block_id, []).append(proposal)
    orders = []
    for block, kind in targets:
        provenance = block["provenance"][0]
        page = provenance["page"]
        bbox = provenance["bbox"]
        asset = state.assets.get((block.get("asset_refs") or [None])[0])
        chars = chars_in_bbox(chars_by_page.get(page, []), bbox, state.policy)
        if not any(char.payload.strip() for char in chars):
            continue  # degenerate crop: no glyph evidence to audit against
        block_attempts = attempts.get(block["block_id"], [])
        orders.append(
            {
                "work_order_id": stable_id("wo", block["block_id"], state.source_sha256),
                "block_id": block["block_id"],
                "type": kind,
                "source_sha256": state.source_sha256,
                "run_id": state.run_id,
                "issues": [code for code in block.get("issues", []) if code in TARGET_ISSUE_TYPES],
                "page": page,
                "bbox": bbox,
                "crop": (
                    {
                        "asset_id": asset["asset_id"],
                        "path": asset["path"],
                        "sha256": asset["sha256"],
                    }
                    if asset
                    else None
                ),
                "caption": captions.get(block["block_id"]),
                "context_text": (block.get("raw_text") or "")[:400],
                "region_glyph_count": sum(1 for char in chars if char.payload.strip()),
                "glyphs": [
                    [
                        char.payload,
                        round(char.bbox[0], 2),
                        round(char.bbox[1], 2),
                        round(char.bbox[2], 2),
                        round(char.bbox[3], 2),
                    ]
                    for char in chars
                ],
                "attempts": {
                    "count": len(block_attempts),
                    "accepted": sum(item.status == "accepted" for item in block_attempts),
                    "last_status": block_attempts[-1].status if block_attempts else None,
                    "last_reject_reasons": (
                        block_attempts[-1].validation.get("reject_reasons", [])
                        if block_attempts and block_attempts[-1].status == "rejected"
                        else []
                    ),
                },
            }
        )
    return orders


def _target_type(block: dict[str, Any]) -> str | None:
    if block.get("status") != "unresolved" or not block.get("asset_refs"):
        return None
    for code in block.get("issues", []):
        if code in TARGET_ISSUE_TYPES:
            if code == "PDF_EQUATION_UNRESOLVED" and block["kind"] not in {"paragraph", "equation"}:
                return None
            return TARGET_ISSUE_TYPES[code]
    return None


def _caption_index(
    state: _PdfState, blocks_by_id: dict[str, dict[str, Any]]
) -> dict[str, dict[str, str]]:
    captions: dict[str, dict[str, str]] = {}
    for relation in state.relations:
        if relation.get("type") != "caption_of":
            continue
        text = " ".join(
            (blocks_by_id.get(block_id, {}).get("text") or "").strip()
            for block_id in relation.get("from_block_ids", [])
        ).strip()
        for block_id in relation.get("to_block_ids", []):
            if text:
                captions[block_id] = {"block_id": relation["from_block_ids"][0], "text": text[:400]}
    return captions


def _plan_proposals(root: Path, draft: Path, adapter: str, model: str) -> list[AuditProposal]:
    """Validate every draft line; raise on schema errors, record semantic rejects."""
    state = _load_pdf_state(root)
    ledger = _load_proposals(root)
    orders = {order["work_order_id"]: order for order in _work_orders(state, ledger)}
    blocks_by_id = {block["block_id"]: block for block in state.blocks}
    chars_by_page: dict[int, list[RawPdfObject]] = {}
    last_any: dict[tuple[str, str], AuditProposal] = {}
    last_accepted: dict[tuple[str, str], AuditProposal] = {}
    for proposal in ledger:
        key = (proposal.block_id, proposal.type)
        last_any[key] = proposal
        if proposal.status == "accepted":
            last_accepted[key] = proposal
    known_ids = {proposal.proposal_id for proposal in ledger}
    draft_seen: set[tuple[str, str]] = set()

    planned: list[AuditProposal] = []
    for number, line in enumerate(draft.read_text(encoding="utf-8").splitlines(), 1):
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"{draft}:{number}: invalid JSON: {error.msg}") from error
        if not isinstance(raw, dict) or raw.get("type") not in PROPOSAL_TYPES:
            raise ValueError(f"{draft}:{number}: expected a table_grid or equation_latex proposal")
        order = orders.get(raw.get("work_order_id"))
        if order is None:
            raise ValueError(f"{draft}:{number}: unknown work order {raw.get('work_order_id')!r}")
        if raw["type"] == "table_grid":
            if set(raw) != TABLE_KEYS:
                raise ValueError(f"{draft}:{number}: expected exactly {sorted(TABLE_KEYS)}")
            payload = {"grid": raw["grid"], "header_rows": raw["header_rows"]}
        else:
            if set(raw) != EQUATION_KEYS:
                raise ValueError(f"{draft}:{number}: expected exactly {sorted(EQUATION_KEYS)}")
            payload = {"latex": raw["latex"]}

        block = blocks_by_id[order["block_id"]]
        reject_reasons: list[str] = []
        validation: dict[str, Any] = {}
        if order["type"] != raw["type"]:
            reject_reasons.append(f"work order expects {order['type']}, not {raw['type']}")
        elif block["status"] != "unresolved":
            reject_reasons.append("target block is no longer unresolved")
        else:
            page = block["provenance"][0]["page"]
            if page not in chars_by_page:
                chars_by_page = page_chars(state.run_root, {page})
            region_chars = chars_in_bbox(chars_by_page[page], block["provenance"][0]["bbox"], state.policy)
            if raw["type"] == "table_grid":
                validation = _validate_table_grid(payload, region_chars, state.policy, block["provenance"][0]["bbox"])
            else:
                validation = _validate_equation_latex(payload, region_chars)
            reject_reasons.extend(validation.pop("reject_reasons"))
        key = (order["block_id"], raw["type"])
        fingerprint = (order["work_order_id"], raw["type"], _canonical(payload))
        if fingerprint in draft_seen:
            raise ValueError(f"{draft}:{number}: duplicate proposal in one draft")
        draft_seen.add(fingerprint)
        revision = (last_any[key].revision + 1) if key in last_any else 1
        proposal_id = stable_id("ap", order["work_order_id"], _canonical(payload), str(revision))
        if proposal_id in known_ids:
            raise ValueError(f"{draft}:{number}: identical proposal already recorded: {proposal_id}")
        proposal = AuditProposal(
            proposal_id=proposal_id,
            work_order_id=order["work_order_id"],
            block_id=order["block_id"],
            type=raw["type"],
            source_sha256=state.source_sha256,
            run_id=state.run_id,
            payload=payload,
            adapter=adapter,
            model=model,
            revision=revision,
            supersedes=last_accepted[key].proposal_id if key in last_accepted else None,
            status="rejected" if reject_reasons else "accepted",
            validation={**validation, "reject_reasons": reject_reasons},
            created_at=_now(),
        )
        known_ids.add(proposal_id)
        last_any[key] = proposal
        if proposal.status == "accepted":
            last_accepted[key] = proposal
        planned.append(proposal)
    return planned


def _validate_table_grid(
    payload: dict[str, Any],
    region_chars: list[RawPdfObject],
    policy: dict[str, Any],
    bbox: list[float],
) -> dict[str, Any]:
    reasons: list[str] = []
    grid_data = payload.get("grid")
    header_rows = payload.get("header_rows")
    if not isinstance(grid_data, dict) or set(grid_data) != {"x_bounds", "y_bounds"}:
        return {"reject_reasons": ["grid must hold exactly x_bounds and y_bounds"]}
    x_bounds, y_bounds = grid_data["x_bounds"], grid_data["y_bounds"]
    for name, bounds in (("x_bounds", x_bounds), ("y_bounds", y_bounds)):
        if (
            not isinstance(bounds, list)
            or len(bounds) < 2
            or not all(isinstance(value, (int, float)) for value in bounds)
            or any(bounds[index] >= bounds[index + 1] for index in range(len(bounds) - 1))
        ):
            reasons.append(f"{name} must be a strictly increasing list of at least two numbers")
    if reasons:
        return {"reject_reasons": reasons}
    if not isinstance(header_rows, int) or isinstance(header_rows, bool) or header_rows < 0:
        reasons.append("header_rows must be a non-negative integer")
        return {"reject_reasons": reasons}
    grid = TableGrid(tuple(float(value) for value in x_bounds), tuple(float(value) for value in y_bounds), header_rows)
    if header_rows >= grid.rows:
        reasons.append("header_rows must be smaller than the row count")
        return {"reject_reasons": reasons}
    tolerance = float(policy["table_cell_overlap_tolerance_pt"])
    if (
        x_bounds[0] < bbox[0] - tolerance
        or x_bounds[-1] > bbox[2] + tolerance
        or y_bounds[0] < bbox[1] - tolerance
        or y_bounds[-1] > bbox[3] + tolerance
    ):
        reasons.append("grid extent leaves the target region bbox")
        return {"reject_reasons": reasons}
    glyphs = [char for char in region_chars if char.payload.strip()]
    excluded = sum(
        1
        for char in glyphs
        if not (
            x_bounds[0] - tolerance <= char.bbox[0]
            and char.bbox[2] <= x_bounds[-1] + tolerance
            and y_bounds[0] - tolerance <= char.bbox[1]
            and char.bbox[3] <= y_bounds[-1] + tolerance
        )
    )
    if excluded:
        reasons.append(
            f"grid extent excludes {excluded} of {len(glyphs)} region glyphs; "
            "every glyph must fall inside the proposed grid"
        )
        return {"reject_reasons": reasons}
    assignment = assign_chars_to_cells(region_chars, grid, policy)
    if not verified(grid, assignment, policy):
        reasons.append(
            f"cell coverage {assignment.coverage:.4f} below policy minimum "
            f"or ambiguous ({assignment.outside} chars outside cells)"
        )
    return {
        "coverage": round(assignment.coverage, 6),
        "outside": assignment.outside,
        "rows": grid.rows,
        "columns": grid.columns,
        "header_rows": header_rows,
        "reject_reasons": reasons,
    }


def _validate_equation_latex(
    payload: dict[str, Any], region_chars: list[RawPdfObject]
) -> dict[str, Any]:
    reasons: list[str] = []
    latex = payload.get("latex")
    glyphs = [char.payload for char in region_chars if char.payload.strip()]
    if not isinstance(latex, str) or not latex.strip():
        reasons.append("latex must be a non-empty string")
    else:
        if not latex_balanced(latex):
            reasons.append("latex delimiters are unbalanced")
        if "(cid:" in latex:
            reasons.append("latex contains unresolved (cid:) glyphs")
        available = Counter(glyphs)
        for glyph, needed in sorted(available.items()):
            escaped = latex_char(glyph)
            consumed = latex.count(glyph) + (latex.count(escaped) if escaped and escaped != glyph else 0)
            if consumed < needed:
                reasons.append(f"latex does not consume region glyph {glyph!r} x{needed}")
    return {
        "glyphs": len(glyphs),
        "distinct_glyphs": len(set(glyphs)),
        "reject_reasons": reasons,
    }


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now() -> str:
    return datetime.now(UTC).isoformat()
