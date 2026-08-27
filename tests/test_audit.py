"""Audit-door contract tests: work orders out, validated proposals in."""

import json
from pathlib import Path

import pytest

pytest.importorskip("pdfplumber")
pytest.importorskip("pypdfium2")

from test_pdf_import import _equation_pdf, _new_project, _table_paper_pdf

from paperweaver.audit import (
    audit_status,
    chars_for_block,
    export_audit_package,
    import_audit_proposals,
)
from paperweaver.cli import run
from paperweaver.core import import_paper


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
    xs = _bounds([_center(char)[0] for char in chars], columns)
    ys = _bounds([_center(char)[1] for char in chars], rows)
    return {
        "work_order_id": order["work_order_id"],
        "type": "table_grid",
        "grid": {"x_bounds": xs, "y_bounds": ys},
        "header_rows": header_rows,
    }


def _bounds(centers: list[float], parts: int) -> list[float]:
    values = sorted(centers)
    gaps = sorted(
        ((values[index + 1] - values[index], index) for index in range(len(values) - 1)),
        reverse=True,
    )[: parts - 1]
    cuts = sorted(values[index + 1] - gap / 2 for gap, index in gaps)
    return [values[0] - 1.0] + cuts + [values[-1] + 1.0]


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
