"""Audit-door contract tests: work orders out, validated proposals in."""

import json
from pathlib import Path

import pytest

pytest.importorskip("pdfplumber")
pytest.importorskip("pypdfium2")

from test_pdf_import import _equation_pdf, _new_project, _table_paper_pdf

from paperweaver.audit import (
    apply_audit_proposals,
    audit_status,
    chars_for_block,
    export_audit_package,
    import_audit_proposals,
)
from paperweaver.cli import run
from paperweaver.core import import_paper
from paperweaver.pdf_contracts import pdf_status
from paperweaver.publication import render_translation_pdf
from paperweaver.translation import (
    MockTranslationAdapter,
    export_translated_markdown,
    segment_paper,
    translate_paper,
)


def _pdf_project(tmp_path: Path, name: str, builder, **kwargs) -> Path:
    project = _new_project(tmp_path / name)
    source = tmp_path / name / "source.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    builder(source, **kwargs)
    import_paper(project, source)
    return project


def _work_orders(project: Path) -> list[dict]:
    export_audit_package(project)
    package = json.loads(
        (project / "output" / "audit-package.json").read_text(encoding="utf-8")
    )
    return package["work_orders"]


def _order(orders: list[dict], kind: str) -> dict:
    return next(order for order in orders if order["type"] == kind)


def _draft(tmp_path: Path, lines: list[dict]) -> Path:
    import hashlib

    digest = hashlib.md5(
        "\n".join(json.dumps(line, sort_keys=True) for line in lines).encode("utf-8")
    ).hexdigest()[:12]
    path = tmp_path / f"draft-{digest}.jsonl"
    path.write_text(
        "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines), encoding="utf-8"
    )
    return path


def _grid_lines(order: dict, chars, *, columns: int, rows: int, header_rows: int) -> dict:
    return {
        "work_order_id": order["work_order_id"],
        "type": "table_grid",
        "grid": {"x_bounds": _bounds(chars, 0, columns), "y_bounds": _bounds(chars, 1, rows)},
        "header_rows": header_rows,
    }


def _bounds(chars, axis: int, parts: int) -> list[float]:
    """Cut a grid axis at the midpoints of the largest glyph-interval gaps."""
    spans = sorted((char.bbox[axis], char.bbox[axis + 2]) for char in chars)
    gaps = sorted(
        ((spans[index + 1][0] - spans[index][1], index) for index in range(len(spans) - 1)),
        reverse=True,
    )[: parts - 1]
    cuts = sorted(spans[index + 1][0] - gap / 2 for gap, index in gaps)
    return [spans[0][0] - 1.0] + cuts + [spans[-1][1] + 1.0]


def _center(char) -> tuple[float, float]:
    return (char.bbox[0] + char.bbox[2]) / 2, (char.bbox[1] + char.bbox[3]) / 2


def test_audit_export_lists_repairable_blocks(tmp_path: Path) -> None:
    project = _pdf_project(tmp_path, "mixed", _table_paper_pdf, boxed=True, partial_vertical=True)
    output = export_audit_package(project)
    assert output.name == "audit-package.json"
    orders = _work_orders(project)
    table = _order(orders, "table_grid")
    assert table["issues"] == ["PDF_TABLE_UNRESOLVED"]
    assert table["crop"]["path"].startswith("source/assets/sha256-")
    assert "Table 1" in table["caption"]["text"]
    assert table["attempts"]["count"] == 0
    assert table["region_glyph_count"] > 0
    assert len(table["glyphs"]) >= table["region_glyph_count"]
    payload, x0, y0, x1, y1 = table["glyphs"][0]
    assert isinstance(payload, str)
    assert x0 < x1 and y0 < y1
    assert chars_for_block(project, table["block_id"])


def test_table_grid_proposal_is_accepted_with_engine_rebuilt_cells(tmp_path: Path) -> None:
    project = _pdf_project(tmp_path, "table", _table_paper_pdf, boxed=True, partial_vertical=True)
    order = _order(_work_orders(project), "table_grid")
    chars = chars_for_block(project, order["block_id"])
    line = _grid_lines(order, chars, columns=3, rows=3, header_rows=1)
    draft = _draft(tmp_path, [line])
    accepted, rejected = import_audit_proposals(project, draft, "paper-agent", "test-model")
    assert (accepted, rejected) == (1, 0)
    ledger = json.loads(
        (project / "state" / "audit-proposals.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    assert ledger["status"] == "accepted"
    assert ledger["validation"]["coverage"] >= 0.98
    assert (ledger["validation"]["rows"], ledger["validation"]["columns"]) == (3, 3)


def test_table_grid_proposals_are_rejected_with_reasons(tmp_path: Path) -> None:
    project = _pdf_project(tmp_path, "table", _table_paper_pdf, boxed=True, partial_vertical=True)
    order = _order(_work_orders(project), "table_grid")
    chars = chars_for_block(project, order["block_id"])
    good = _grid_lines(order, chars, columns=3, rows=3, header_rows=1)
    bbox = order["bbox"]
    outside = {
        **good,
        "grid": {"x_bounds": [bbox[0] + 500.0, bbox[2] + 600.0], "y_bounds": good["grid"]["y_bounds"]},
    }
    row_centers = sorted({_center(char)[1] for char in chars})
    sliced = {
        **good,
        "grid": {
            "x_bounds": good["grid"]["x_bounds"],
            "y_bounds": [
                good["grid"]["y_bounds"][0],
                row_centers[len(row_centers) // 2] + 1.0,
                good["grid"]["y_bounds"][-1],
            ],
        },
    }
    wrong_type = {"work_order_id": order["work_order_id"], "type": "equation_latex", "latex": "x"}
    draft = _draft(tmp_path, [outside, sliced, wrong_type])
    accepted, rejected = import_audit_proposals(project, draft, "paper-agent", "test-model")
    assert (accepted, rejected) == (0, 3)
    records = [
        json.loads(line)
        for line in (project / "state" / "audit-proposals.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    reasons = " ".join(record["validation"]["reject_reasons"][0] for record in records)
    assert "leaves the target region bbox" in reasons
    assert "outside" in reasons or "coverage" in reasons
    assert "expects table_grid" in reasons


def test_equation_latex_proposal_consumes_every_region_glyph(tmp_path: Path) -> None:
    project = _pdf_project(tmp_path, "equation", _equation_pdf, unsafe_tex=True)
    order = _order(_work_orders(project), "equation_latex")
    chars = chars_for_block(project, order["block_id"])
    glyphs = [char.payload for char in chars if char.payload.strip()]
    good = {
        "work_order_id": order["work_order_id"],
        "type": "equation_latex",
        "latex": "".join(glyphs),
    }
    unbalanced = {**good, "latex": good["latex"] + "("}
    missing = {**good, "latex": good["latex"].replace("%", "")}
    cid = {**good, "latex": "y (cid:12)"}
    draft = _draft(tmp_path, [good, unbalanced, missing, cid])
    accepted, rejected = import_audit_proposals(project, draft, "paper-agent", "test-model")
    assert (accepted, rejected) == (1, 3)
    records = [
        json.loads(line)
        for line in (project / "state" / "audit-proposals.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    statuses = [record["status"] for record in records]
    assert statuses == ["accepted", "rejected", "rejected", "rejected"]
    reasons = " ".join(
        " ".join(record["validation"]["reject_reasons"]) for record in records[1:]
    )
    assert "unbalanced" in reasons
    assert "does not consume region glyph '%'" in reasons
    assert "(cid:)" in reasons


def test_verify_draft_writes_nothing_and_reports_verdicts(tmp_path: Path, capsys) -> None:
    project = _pdf_project(tmp_path, "table", _table_paper_pdf, boxed=True, partial_vertical=True)
    order = _order(_work_orders(project), "table_grid")
    chars = chars_for_block(project, order["block_id"])
    good = _grid_lines(order, chars, columns=3, rows=3, header_rows=1)
    bad = {**good, "grid": {"x_bounds": [1.0, 2.0], "y_bounds": good["grid"]["y_bounds"]}}
    draft = _draft(tmp_path, [good, bad])
    assert run(["verify-draft", str(project), str(draft)]) == 1
    output = capsys.readouterr().out
    assert "1 accepted" in output and "2 rejected" in output
    assert not (project / "state" / "audit-proposals.jsonl").exists()


def test_resubmission_supersedes_and_duplicates_are_refused(tmp_path: Path) -> None:
    project = _pdf_project(tmp_path, "table", _table_paper_pdf, boxed=True, partial_vertical=True)
    order = _order(_work_orders(project), "table_grid")
    chars = chars_for_block(project, order["block_id"])
    first = _grid_lines(order, chars, columns=3, rows=3, header_rows=1)
    assert import_audit_proposals(project, _draft(tmp_path, [first]), "a", "m1") == (1, 0)
    gap_left = first["grid"]["x_bounds"][1]
    next_center = min(_center(char)[0] for char in chars if _center(char)[0] > gap_left)
    refined = {
        **first,
        "grid": {
            **first["grid"],
            "x_bounds": sorted({*first["grid"]["x_bounds"], (gap_left + next_center) / 2}),
        },
    }
    assert refined["grid"]["x_bounds"] != first["grid"]["x_bounds"]
    assert len(refined["grid"]["x_bounds"]) == len(first["grid"]["x_bounds"]) + 1
    assert import_audit_proposals(project, _draft(tmp_path, [refined]), "a", "m2") == (1, 0)
    records = [
        json.loads(line)
        for line in (project / "state" / "audit-proposals.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [record["revision"] for record in records] == [1, 2]
    assert records[1]["supersedes"] == records[0]["proposal_id"]
    assert records[1]["model"] == "m2"
    with pytest.raises(ValueError, match="duplicate proposal in one draft"):
        import_audit_proposals(project, _draft(tmp_path, [first, first]), "a", "m1")


def test_audit_status_reports_burn_down(tmp_path: Path) -> None:
    project = _pdf_project(tmp_path, "mixed", _table_paper_pdf, boxed=True, partial_vertical=True)
    table = _order(_work_orders(project), "table_grid")
    chars = chars_for_block(project, table["block_id"])
    good = _grid_lines(table, chars, columns=3, rows=3, header_rows=1)
    bad = {**good, "header_rows": 99}
    import_audit_proposals(project, _draft(tmp_path, [good, bad]), "a", "m")
    status = audit_status(project)
    assert status["targets"] >= 1
    assert status["accepted"] == 1
    assert status["attempted_not_accepted"] == 0
    assert status["untouched"] == status["targets"] - 1
    assert status["proposals"] == 2
    assert status["acceptance_rate"] == 0.5


def test_audit_cli_commands_round_trip(tmp_path: Path, capsys) -> None:
    project = _pdf_project(tmp_path, "table", _table_paper_pdf, boxed=True, partial_vertical=True)
    assert run(["audit-export", str(project)]) == 0
    capsys.readouterr()
    assert run(["audit-status", str(project)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["targets"] >= 1
    with pytest.raises(OSError):
        run(["audit-import", str(project), str(project / "missing.jsonl"), "--model", "m"])


def test_audit_rejects_non_pdf_project(tmp_path: Path) -> None:
    project = _new_project(tmp_path / "plain")
    source = project / "paper.md"
    source.write_text("# 标题\n\n正文段落。\n", encoding="utf-8")
    import_paper(project, source)
    with pytest.raises(ValueError, match="AUDIT_INVALID"):
        export_audit_package(project)


def _table_and_equation_pdf(path: Path) -> None:
    """One page holding both an unresolved boxed table and an unsafe equation."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen.canvas import Canvas

    document = Canvas(str(path), pagesize=A4, invariant=True)
    _, height = A4
    document.setFont("Helvetica-Bold", 17)
    document.drawString(42, height - 55, "Mixed Repairs Study")
    document.setFont("Helvetica-Bold", 11)
    document.drawString(42, height - 95, "Methods")
    document.setFont("Helvetica", 9)
    document.drawString(
        42, height - 120, "The display equation below uses selectable baseline and subscript characters."
    )
    y = height - 180
    document.setFont("Times-Italic", 10)
    document.drawString(80, y, "y")
    document.setFont("Times-Italic", 6)
    document.drawString(86, y - 3, "it")
    document.setFont("Times-Roman", 10)
    document.drawString(102, y, "=")
    document.setFont("Times-Italic", 10)
    document.drawString(120, y, "b")
    document.setFont("Times-Roman", 6)
    document.drawString(126, y - 3, "1")
    document.setFont("Times-Italic", 10)
    document.drawString(138, y, "x")
    document.setFont("Times-Italic", 6)
    document.drawString(144, y - 3, "it")
    document.setFont("Times-Roman", 10)
    document.drawString(158, y, "%")
    document.drawString(162, y, "+")
    document.setFont("Times-Italic", 10)
    document.drawString(180, y, "e")
    document.setFont("Times-Italic", 6)
    document.drawString(186, y - 3, "it")
    document.drawString(500, y, "(1)")
    document.setFont("Helvetica", 9)
    document.drawString(42, height - 220, "A narrative line separates the equation from the table below.")
    document.setFont("Times-Roman", 10)
    document.drawString(60, height - 260, "Table 1. Summary statistics")
    top = height - 304
    row_h, col_w, columns, rows = 22, 60, 3, 3
    xs = [60 + i * col_w for i in range(columns + 1)]
    ys = [top - i * row_h for i in range(rows + 1)]
    document.setLineWidth(0.8)
    for grid_y in ys:
        document.line(xs[0], grid_y, xs[-1], grid_y)
    for index, x_value in enumerate(xs):
        if index == columns // 2 + 1:
            document.line(x_value, ys[1], x_value, ys[-1])
            continue
        document.line(x_value, ys[0], x_value, ys[-1])
    for row, cells in enumerate(
        (["Group", "Mean", "SD"], ["A", "4.2", "0.3"], ["B", "5.1", "0.4"])
    ):
        for column, value in enumerate(cells):
            document.setFont("Times-Roman", 9)
            document.drawString(xs[column] + 4, ys[row] - 15, value)
    document.save()


def _accept_grid_proposal(tmp_path: Path, project: Path) -> dict:
    order = _order(_work_orders(project), "table_grid")
    chars = chars_for_block(project, order["block_id"])
    line = _grid_lines(order, chars, columns=3, rows=3, header_rows=1)
    draft = _draft(tmp_path, [line])
    accepted, rejected = import_audit_proposals(project, draft, "paper-agent", "test-model")
    assert (accepted, rejected) == (1, 0)
    return order


def test_audit_apply_materializes_table_and_completes_delivery(tmp_path: Path) -> None:
    project = _pdf_project(tmp_path, "table", _table_paper_pdf, boxed=True, partial_vertical=True)
    _accept_grid_proposal(tmp_path, project)
    result = apply_audit_proposals(project)
    assert result == {"status": "complete_with_repair", "applied": 1, "table_grids": 1, "equation_latex": 0}
    article = (project / "source" / "article.md").read_text(encoding="utf-8")
    assert "| Group | Mean | SD |" in article
    assert "| A | 4.2 | 0.3 |" in article
    assert "[!WARNING]" not in article
    manifest = json.loads((project / "source" / "pdf" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "complete_with_repair"
    assert len(manifest["repairs"]["applied_proposal_ids"]) == 1
    assert pdf_status(project) == "complete_with_repair"
    assert run(["pdf-status", str(project)]) == 0


def test_audit_apply_repaired_table_flows_through_translation(tmp_path: Path) -> None:
    from pypdf import PdfReader

    project = _pdf_project(tmp_path, "flow", _table_paper_pdf, boxed=True, partial_vertical=True)
    _accept_grid_proposal(tmp_path, project)
    apply_audit_proposals(project)
    segment_paper(project, unit_size=1)
    translate_paper(project, MockTranslationAdapter())
    exported = export_translated_markdown(project).read_text(encoding="utf-8")
    assert "[MOCK zh-CN] Group" in exported
    assert "| A |" in exported
    pdf = render_translation_pdf(project / "output" / "translated.md")
    assert pdf.read_bytes().startswith(b"%PDF")
    rendered = " ".join(page.extract_text() or "" for page in PdfReader(pdf).pages)
    assert "Group" in rendered and "4.2" in rendered


def test_audit_apply_keeps_incomplete_when_targets_remain(tmp_path: Path) -> None:
    project = _pdf_project(tmp_path, "mixed", _table_and_equation_pdf)
    orders = _work_orders(project)
    assert {order["type"] for order in orders} == {"table_grid", "equation_latex"}
    _accept_grid_proposal(tmp_path, project)
    result = apply_audit_proposals(project)
    assert result["status"] == "incomplete"
    article = (project / "source" / "article.md").read_text(encoding="utf-8")
    assert "| Group | Mean | SD |" in article
    assert "PDF_EQUATION_UNRESOLVED" in article
    assert pdf_status(project) == "incomplete"
    qa = json.loads((project / "source" / "pdf" / "qa.json").read_text(encoding="utf-8"))
    assert qa["repairs"] == {"tables": 1, "equations": 0, "total": 1}
    assert qa["metrics"]["unresolved_blocks"] == 1


def test_audit_apply_supports_incremental_repairs(tmp_path: Path) -> None:
    """Apply, then accept a new proposal and apply again without fail-closed."""
    project = _pdf_project(tmp_path, "incremental", _table_and_equation_pdf)
    _accept_grid_proposal(tmp_path, project)
    assert apply_audit_proposals(project)["applied"] == 1
    order = _order(_work_orders(project), "equation_latex")
    chars = chars_for_block(project, order["block_id"])
    glyphs = "".join(char.payload for char in chars if char.payload.strip())
    draft = _draft(
        tmp_path,
        [{"work_order_id": order["work_order_id"], "type": "equation_latex", "latex": glyphs}],
    )
    accepted, _ = import_audit_proposals(project, draft, "paper-agent", "test-model")
    assert accepted == 1
    result = apply_audit_proposals(project)
    assert result["applied"] == 2
    assert result["status"] == "complete_with_repair"
    article = (project / "source" / "article.md").read_text(encoding="utf-8")
    assert "| Group | Mean | SD |" in article
    assert "$$" in article
    manifest = json.loads((project / "source" / "pdf" / "manifest.json").read_text())
    assert len(manifest["repairs"]["applied_proposal_ids"]) == 2
    assert pdf_status(project) == "complete_with_repair"


def test_audit_apply_promotes_audited_equation_without_engine_credit(tmp_path: Path) -> None:
    project = _pdf_project(tmp_path, "equation", _equation_pdf, unsafe_tex=True)
    order = _order(_work_orders(project), "equation_latex")
    chars = chars_for_block(project, order["block_id"])
    glyphs = "".join(char.payload for char in chars if char.payload.strip())
    draft = _draft(
        tmp_path,
        [{"work_order_id": order["work_order_id"], "type": "equation_latex", "latex": glyphs}],
    )
    accepted, _ = import_audit_proposals(project, draft, "paper-agent", "test-model")
    assert accepted == 1
    result = apply_audit_proposals(project)
    assert result == {"status": "complete_with_repair", "applied": 1, "table_grids": 0, "equation_latex": 1}
    article = (project / "source" / "article.md").read_text(encoding="utf-8")
    assert "$$" in article
    qa = json.loads((project / "source" / "pdf" / "qa.json").read_text(encoding="utf-8"))
    assert qa["metrics"]["verified_equations"] == 0
    assert qa["repairs"] == {"tables": 0, "equations": 1, "total": 1}
    assert pdf_status(project) == "complete_with_repair"


def test_audit_apply_is_idempotent(tmp_path: Path) -> None:
    project = _pdf_project(tmp_path, "idem", _table_paper_pdf, boxed=True, partial_vertical=True)
    _accept_grid_proposal(tmp_path, project)
    apply_audit_proposals(project)
    tracked = [
        project / "source" / "article.md",
        project / "source" / "article-map.jsonl",
        project / "source" / "pdf" / "render-tree.json",
        project / "source" / "pdf" / "qa.json",
        project / "source" / "pdf" / "manifest.json",
    ]
    before = [path.read_bytes() for path in tracked]
    apply_audit_proposals(project)
    assert [path.read_bytes() for path in tracked] == before


def test_audit_apply_detects_ledger_tamper(tmp_path: Path) -> None:
    project = _pdf_project(tmp_path, "tamper", _table_paper_pdf, boxed=True, partial_vertical=True)
    _accept_grid_proposal(tmp_path, project)
    apply_audit_proposals(project)
    ledger = project / "state" / "audit-proposals.jsonl"
    ledger.write_text(ledger.read_text(encoding="utf-8") + json.dumps({"tampered": True}) + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="PDF_REPAIR_LEDGER_MISMATCH"):
        pdf_status(project)


def test_audit_apply_detects_evidence_mismatch(tmp_path: Path) -> None:
    project = _pdf_project(tmp_path, "mismatch", _table_paper_pdf, boxed=True, partial_vertical=True)
    _accept_grid_proposal(tmp_path, project)
    ledger = project / "state" / "audit-proposals.jsonl"
    records = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    records[0]["validation"]["coverage"] = 0.5
    ledger.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="PDF_REPAIR_EVIDENCE_MISMATCH"):
        apply_audit_proposals(project)


def test_audit_apply_requires_accepted_proposals(tmp_path: Path) -> None:
    project = _pdf_project(tmp_path, "empty", _table_paper_pdf, boxed=True, partial_vertical=True)
    with pytest.raises(ValueError, match="AUDIT_APPLY_NOTHING"):
        apply_audit_proposals(project)
