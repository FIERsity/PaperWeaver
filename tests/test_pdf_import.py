import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("pdfplumber")
pytest.importorskip("pypdfium2")

from PIL import Image, ImageDraw
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

import paperweaver.pdf_import as pdf_import_module
from paperweaver.cli import run
from paperweaver.core import import_paper, init_project
from paperweaver.pdf_backend_pdfplumber import _recover_unmapped_glyph
from paperweaver.pdf_contracts import default_policy
from paperweaver.pdf_markdown import _html_table, _pipe_table
from paperweaver.pdf_table import TableGrid, _table_rules, assign_chars_to_cells, verified
from paperweaver.publication import render_translation_pdf
from paperweaver.translation import (
    MockTranslationAdapter,
    export_translated_markdown,
    segment_paper,
    translate_paper,
)


def _text_pdf(path: Path, *, columns: bool = False, visual: bool = False) -> None:
    document = canvas.Canvas(str(path), pagesize=A4, invariant=True)
    width, height = A4
    pages = 3 if columns else 1
    for page in range(1, pages + 1):
        document.setFont("Helvetica", 8)
        if columns:
            document.drawString(42, height - 28, "Running Journal Header 2026")
        if page == 1:
            document.setFont("Helvetica-Bold", 17)
            document.drawString(42, height - 72, "A Traceable PDF Study")
            document.setFont("Helvetica-Bold", 11)
            document.drawString(42, height - 112, "Abstract")
        document.setFont("Helvetica", 10)
        if columns and page == 1:
            left = [
                "Left column first paragraph.",
                "Left column second paragraph.",
                "Left column final paragraph.",
            ]
            right = [
                "Right column first paragraph.",
                "Right column second paragraph.",
                "Right column final paragraph.",
            ]
            for index, value in enumerate(left):
                document.drawString(42, height - 155 - index * 22, value)
            for index, value in enumerate(right):
                document.drawString(315, height - 155 - index * 22, value)
        else:
            y = height - (145 if page == 1 else 72)
            for value in (
                "This born digital page contains selectable text and explicit provenance.",
                "The importer must preserve every character and reconstruct readable paragraphs.",
                "Repeated execution must keep source digests and stable block identities unchanged.",
            ):
                document.drawString(54, y, value)
                y -= 18
        if page == 1:
            document.setFont("Helvetica-Bold", 11)
            document.drawString(42, height - 260, "1. Methods")
            document.setFont("Helvetica", 10)
            document.drawString(
                54,
                height - 282,
                "We use deterministic extraction and refuse incomplete content.",
            )
        if visual and page == 1:
            document.setLineWidth(2)
            document.line(42, height - 330, width - 42, height - 330)
        document.setFont("Helvetica", 8)
        document.drawCentredString(width / 2, 24, str(page))
        document.showPage()
    document.save()


def _new_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    init_project(project, "测试论文", "en", "zh-CN")
    return project


def test_raster_crop_box_preserves_subpixel_evidence_at_page_edges() -> None:
    assert pdf_import_module._raster_crop_box(
        [346.564, 347.924, 398.721, 347.934], 2.0, (1224, 1584)
    ) == (693, 695, 798, 696)
    assert pdf_import_module._raster_crop_box(
        [611.99, 791.99, 612.0, 792.0], 2.0, (1224, 1584)
    ) == (1223, 1583, 1224, 1584)


def test_table_rule_growth_does_not_cross_filled_chart_bars() -> None:
    policy = default_policy()
    caption = [50.0, 80.0, 250.0, 90.0]
    table_rule = SimpleNamespace(kind="line", bbox=[50.0, 100.0, 250.0, 100.1])
    filled_bar = SimpleNamespace(kind="rect", bbox=[100.0, 100.0, 110.0, 200.0])
    chart_rule = SimpleNamespace(kind="line", bbox=[50.0, 200.0, 250.0, 200.1])
    selected = _table_rules([table_rule, filled_bar, chart_rule], caption, policy)
    assert selected == [table_rule]


def test_table_cell_text_uses_geometry_not_backend_stream_order() -> None:
    policy = default_policy()
    grid = TableGrid((0.0, 100.0), (0.0, 50.0), 1)

    def char(ref: str, value: str, x: float, y: float):
        return SimpleNamespace(
            kind="char",
            payload=value,
            object_ref=ref,
            bbox=[x, y, x + 4.0, y + 6.0],
            attrs={"size": 8.0},
        )

    chars = [
        char("d", "D", 12.5, 20.0),
        char("b", "B", 12.5, 8.0),
        char("c", "C", 8.0, 20.0),
        char("a", "A", 8.0, 8.0),
    ]
    assignment = assign_chars_to_cells(chars, grid, policy)
    assert assignment.cells == (("AB\nCD",),)
    assert assignment.object_refs == ((("a", "b", "c", "d"),),)
    assert verified(grid, assignment, policy)


def _table_paper_pdf(
    path: Path,
    *,
    boxed: bool = True,
    partial_vertical: bool = False,
) -> None:
    """Draw a title, abstract, body, and a Table 1 (optionally boxed) on one page."""
    document = canvas.Canvas(str(path), pagesize=A4, invariant=True)
    _, height = A4
    document.setFont("Times-Bold", 20)
    document.drawString(60, height - 60, "A Traceable Boxed Table Study")
    document.setFont("Times-Roman", 10)
    document.drawString(60, height - 96, "Abstract")
    document.setFont("Times-Roman", 9)
    document.drawString(60, height - 110, "This paper establishes a deterministic boxed table import pathway.")
    document.drawString(60, height - 126, "We separate structured tables from body text so translation stays on prose.")
    document.setFont("Times-Roman", 10)
    document.drawString(60, height - 150, "1. Methods")
    document.setFont("Times-Roman", 9)
    document.drawString(60, height - 166, "We reconstruct grid tables from ruled geometry when every cell accounts.")
    document.setFont("Times-Roman", 10)
    document.drawString(60, height - 188, "Table 1. Summary statistics")
    top = height - 232
    row_h, col_w, columns, rows = 22, 60, 3, 3
    xs = [60 + i * col_w for i in range(columns + 1)]
    ys = [top - i * row_h for i in range(rows + 1)]
    if boxed:
        document.setLineWidth(0.8)
        for y in ys:
            document.line(xs[0], y, xs[-1], y)
        for index, x_value in enumerate(xs):
            if partial_vertical and index == (columns // 2 + 1):
                # A mid border present in only some rows is a partial rule -> span.
                document.line(x_value, ys[1], x_value, ys[-1])
                continue
            document.line(x_value, ys[0], x_value, ys[-1])
    document.setFont("Times-Roman", 8)
    headers = ["Group", "Mean", "SD"][:columns]
    for index, header in enumerate(headers):
        document.drawString(xs[index] + 8, ys[0] - row_h + 6, header)
    data = [["A", "4.2", "0.3"], ["B", "5.1", "0.4"]][: rows - 1]
    for row_index, row in enumerate(data):
        for column_index, value in enumerate(row[:columns]):
            document.drawString(xs[column_index] + 8, ys[row_index + 1] - row_h + 6, value)
    document.showPage()
    document.save()


def _table_article(project: Path) -> str:
    return (project / "source" / "article.md").read_text(encoding="utf-8")


def _three_column_pdf(path: Path) -> None:
    document = canvas.Canvas(str(path), pagesize=A4, invariant=True)
    _, height = A4
    document.setFont("Helvetica-Bold", 17)
    document.drawString(42, height - 60, "Three Column Layout")
    document.setFont("Helvetica", 9)
    for row in range(9):
        y = height - 110 - row * 22
        document.drawString(42, y, f"Left {row} content block")
        document.drawString(225, y, f"Middle {row} content block")
        document.drawString(410, y, f"Right {row} content block")
    document.save()


def _mixed_band_two_column_pdf(path: Path) -> None:
    document = canvas.Canvas(str(path), pagesize=A4, invariant=True)
    _, height = A4
    document.setFont("Helvetica-Bold", 17)
    document.drawString(42, height - 55, "Mixed Band Two Column Study")
    document.setFont("Helvetica", 9)
    for row in range(4):
        y = height - 100 - row * 18
        document.drawString(42, y, f"Upper left paragraph line {row} with source evidence.")
        document.drawString(315, y, f"Upper right paragraph line {row} with source evidence.")
    document.setFont("Helvetica-Bold", 12)
    document.drawString(42, height - 190, "2. Results")
    document.setFont("Helvetica", 9)
    for row in range(4):
        y = height - 230 - row * 18
        document.drawString(42, y, f"Lower left paragraph line {row} with source evidence.")
        document.drawString(315, y, f"Lower right paragraph line {row} with source evidence.")
    document.save()


def _repeated_visual_header_and_link_pdf(path: Path) -> None:
    document = canvas.Canvas(str(path), pagesize=A4, invariant=True)
    _, height = A4
    for page in range(1, 4):
        document.setFillColorRGB(0.1, 0.2, 0.4)
        document.rect(24, height - 48, 20, 20, stroke=0, fill=1)
        document.setFillColorRGB(0, 0, 0)
        if page == 1:
            document.setFont("Helvetica-Bold", 17)
            document.drawString(60, height - 72, "Repeated Visual Artifact Study")
            document.setFont("Helvetica-Bold", 11)
            document.drawString(60, height - 110, "Introduction")
        document.setFont("Helvetica", 10)
        document.drawString(
            60,
            height - 145,
            f"Page {page} contains selectable source text with deterministic provenance.",
        )
        document.drawString(
            60,
            height - 163,
            "Repeated header artwork and link decorations are not paper findings.",
        )
        if page == 1:
            document.drawString(60, height - 190, "Repository")
            document.linkURL(
                "https://example.test/repository",
                (60, height - 193, 110, height - 181),
                relative=0,
            )
            document.setFillColorRGB(0.2, 0.2, 0.2)
            document.rect(113, height - 191, 4, 4, stroke=0, fill=1)
            document.setFillColorRGB(0, 0, 0)
        document.showPage()
    document.save()


def _two_figure_pdf(path: Path) -> None:
    document = canvas.Canvas(str(path), pagesize=A4, invariant=True)
    _, height = A4
    document.setFont("Helvetica-Bold", 17)
    document.drawString(42, height - 55, "Two Figure Study")
    for top, number in ((height - 100, 1), (height - 360, 2)):
        left, right, bottom = 70, 500, top - 130
        document.setLineWidth(0.5)
        for index in range(6):
            y = bottom + index * 24
            document.line(left, y, right, y)
        for index in range(8):
            x = left + index * 60
            document.line(x, bottom, x, top)
        document.setLineWidth(1.2)
        document.line(left, bottom + 18, right, top - 20)
        document.setFont("Helvetica", 7)
        document.drawString(80, bottom + 8, f"Axis label {number}")
        document.setFont("Helvetica", 9)
        document.drawString(70, bottom - 18, f"Fig. {number}. Deterministic panel {number}.")
    document.setFont("Helvetica-Bold", 11)
    document.drawString(42, 105, "Conclusion")
    document.setFont("Helvetica", 9)
    document.drawString(
        42,
        88,
        "Both figure regions remain separate, traceable, and structurally ordered.",
    )
    document.save()


def _nearby_unboxed_table_and_chart_pdf(path: Path) -> None:
    document = canvas.Canvas(str(path), pagesize=A4, invariant=True)
    _, height = A4
    document.setFont("Helvetica-Bold", 17)
    document.drawString(42, height - 48, "Nearby Table and Figure Ownership")
    document.setLineWidth(0.7)
    document.line(60, height - 112, 535, height - 112)
    document.line(60, height - 148, 535, height - 148)
    document.setFont("Helvetica", 8)
    document.drawString(75, height - 132, "Task family")
    document.drawString(220, height - 132, "Input")
    document.drawString(355, height - 132, "Metric")
    document.setFont("Helvetica", 9)
    document.drawString(60, height - 170, "Table 1: Dataset descriptions.")
    document.setLineWidth(0.5)
    for offset in (202, 218, 234):
        document.line(82, height - offset, 470, height - offset)
    for index, value in enumerate((18, 34, 50, 66)):
        document.setFillColorRGB(0.2, 0.4 + index * 0.1, 0.8)
        document.rect(105 + index * 70, height - 258, 28, value, stroke=0, fill=1)
    document.setFillColorRGB(0, 0, 0)
    document.setFont("Helvetica", 7)
    document.drawString(105, height - 244, "ZXChart selectable label")
    document.setFont("Helvetica", 9)
    document.drawString(60, height - 286, "Figure 1: Performance comparison by dataset.")
    document.setFont("Helvetica", 9)
    document.drawString(
        60,
        height - 320,
        "The unresolved unboxed table must not claim the neighboring chart evidence.",
    )
    document.save()


def _equation_pdf(path: Path, *, unsafe_tex: bool = False) -> None:
    document = canvas.Canvas(str(path), pagesize=A4, invariant=True)
    _, height = A4
    document.setFont("Helvetica-Bold", 17)
    document.drawString(42, height - 55, "Verified Equation Study")
    document.setFont("Helvetica-Bold", 11)
    document.drawString(42, height - 95, "Methods")
    document.setFont("Helvetica", 9)
    document.drawString(
        42,
        height - 120,
        "The display equation below uses selectable baseline and subscript characters.",
    )
    document.drawString(
        42,
        height - 136,
        "Every source glyph must belong either to the equation or to surrounding prose.",
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
    if unsafe_tex:
        document.setFont("Times-Roman", 10)
        document.drawString(158, y, "%")
    document.setFont("Times-Roman", 10)
    document.drawString(162, y, "+")
    document.setFont("Times-Italic", 10)
    document.drawString(180, y, "e")
    document.setFont("Times-Italic", 6)
    document.drawString(186, y - 3, "it")
    document.setFont("Times-Roman", 10)
    document.drawString(500, y, "(1)")
    document.setFont("Helvetica", 9)
    document.drawString(
        42,
        height - 220,
        "The following paragraph remains ordinary translatable narrative after extraction.",
    )
    document.save()


def test_pdf_preserves_original_digest_and_is_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    _text_pdf(source)
    project = _new_project(tmp_path)
    imported = import_paper(project, source)
    data = source.read_bytes()
    assert imported.sha256 == hashlib.sha256(data).hexdigest()
    assert (project / "source" / "original.pdf").read_bytes() == data
    article_before = (project / "source" / "article.md").read_bytes()
    assert import_paper(project, source) == imported
    assert (project / "source" / "article.md").read_bytes() == article_before

    replacement = tmp_path / "replacement.pdf"
    _text_pdf(replacement, visual=True)
    with pytest.raises(FileExistsError, match="different source"):
        import_paper(project, replacement)


def test_single_column_pdf_materializes_traceable_markdown(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    _text_pdf(source)
    project = _new_project(tmp_path)
    import_paper(project, source)
    manifest = json.loads(
        (project / "source" / "pdf" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] in {"complete", "complete_with_warnings"}
    article = (project / "source" / "article.md").read_text(encoding="utf-8")
    assert "# A Traceable PDF Study" in article
    assert "<!-- paperweaver:block blk_" in article
    passages, _ = segment_paper(project, unit_size=1)
    assert passages
    assert all("paperweaver:block" not in item.text for item in passages)
    assert all("pdf:p" in item.source_locator for item in passages)
    provenance = (project / "state" / "passage-provenance.jsonl").read_text(
        encoding="utf-8"
    )
    assert all(item.id in provenance for item in passages)


def test_two_column_order_and_running_artifacts(tmp_path: Path) -> None:
    source = tmp_path / "columns.pdf"
    _text_pdf(source, columns=True)
    project = _new_project(tmp_path)
    import_paper(project, source)
    article = (project / "source" / "article.md").read_text(encoding="utf-8")
    assert article.index("Left column final") < article.index("Right column first")
    assert "Running Journal Header" not in article
    run_id = json.loads(
        (project / "source" / "pdf" / "manifest.json").read_text(encoding="utf-8")
    )["active_run_id"]
    blocks = (project / "source" / "pdf" / "runs" / run_id / "base-blocks.jsonl").read_text(
        encoding="utf-8"
    )
    assert '"kind": "header"' in blocks
    assert '"disposition": "excluded_artifact"' in blocks


def test_two_column_bands_are_split_by_full_width_section_heading(tmp_path: Path) -> None:
    source = tmp_path / "mixed-bands.pdf"
    _mixed_band_two_column_pdf(source)
    project = _new_project(tmp_path)
    import_paper(project, source)
    article = (project / "source" / "article.md").read_text(encoding="utf-8")
    assert article.index("Upper left paragraph line 3") < article.index(
        "Upper right paragraph line 0"
    )
    assert article.index("Upper right paragraph line 3") < article.index("## 2. Results")
    assert article.index("## 2. Results") < article.index("Lower left paragraph line 0")
    assert article.index("Lower left paragraph line 3") < article.index(
        "Lower right paragraph line 0"
    )


def test_repeated_visual_headers_and_link_icons_are_explicit_artifacts(
    tmp_path: Path,
) -> None:
    source = tmp_path / "visual-artifacts.pdf"
    _repeated_visual_header_and_link_pdf(source)
    project = _new_project(tmp_path)
    import_paper(project, source)
    qa = json.loads((project / "source" / "pdf" / "qa.json").read_text(encoding="utf-8"))
    assert qa["status"] in {"complete", "complete_with_warnings"}
    manifest = json.loads(
        (project / "source" / "pdf" / "manifest.json").read_text(encoding="utf-8")
    )
    accounting = [
        json.loads(line)
        for line in (
            project
            / "source"
            / "pdf"
            / "runs"
            / manifest["active_run_id"]
            / "object-accounting.jsonl"
        )
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    reasons = {item["reason_code"] for item in accounting}
    assert "PDF_REPEATED_VISUAL_HEADER_FOOTER" in reasons
    assert "PDF_LINK_DECORATION" in reasons


def test_visible_non_text_region_is_unresolved_and_blocks_segment(tmp_path: Path) -> None:
    source = tmp_path / "visual.pdf"
    _text_pdf(source, visual=True)
    project = _new_project(tmp_path)
    import_paper(project, source)
    manifest = json.loads(
        (project / "source" / "pdf" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == "incomplete"
    qa = json.loads((project / "source" / "pdf" / "qa.json").read_text(encoding="utf-8"))
    assert any(item["code"] == "PDF_VISIBLE_REGION_UNRESOLVED" for item in qa["issues"])
    assets = list((project / "source" / "assets").glob("sha256-*.png"))
    assert assets and assets[0].read_bytes().startswith(b"\x89PNG")
    with pytest.raises(RuntimeError, match="PDF_IMPORT_INCOMPLETE"):
        segment_paper(project)
    assert run(["pdf-validate", str(project)]) == 2


def test_pdf_raw_object_accounting_is_total(tmp_path: Path) -> None:
    source = tmp_path / "visual.pdf"
    _text_pdf(source, visual=True)
    project = _new_project(tmp_path)
    import_paper(project, source)
    manifest = json.loads(
        (project / "source" / "pdf" / "manifest.json").read_text(encoding="utf-8")
    )
    run_root = project / "source" / "pdf" / "runs" / manifest["active_run_id"]
    raw = [json.loads(line) for line in (run_root / "raw-objects.jsonl").read_text().splitlines()]
    accounting = [
        json.loads(line)
        for line in (run_root / "object-accounting.jsonl").read_text().splitlines()
    ]
    assert len(raw) == len(accounting)
    assert {item["object_ref"] for item in raw} == {
        item["object_ref"] for item in accounting
    }


def test_pdf_cli_status_exit_codes(tmp_path: Path, capsys) -> None:
    source = tmp_path / "paper.pdf"
    _text_pdf(source)
    project = _new_project(tmp_path)
    assert run(["import", str(project), str(source)]) == 0
    assert run(["pdf-status", str(project)]) == 0
    assert capsys.readouterr().out.strip() in {"complete", "complete_with_warnings"}


def test_txt_reimport_compares_original_digest(tmp_path: Path) -> None:
    source = tmp_path / "paper.txt"
    source.write_text("TITLE: Study\n\n1 Introduction\n\nText.\n", encoding="utf-8")
    project = _new_project(tmp_path)
    first = import_paper(project, source)
    assert import_paper(project, source) == first


def test_pdf_gate_rejects_article_and_manifest_tampering(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    _text_pdf(source)
    project = _new_project(tmp_path)
    import_paper(project, source)
    article = project / "source" / "article.md"
    article.write_text(
        article.read_text(encoding="utf-8") + "\nInjected unlocated claim.\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="PDF_ARTICLE_DIGEST_MISMATCH"):
        segment_paper(project)

    visual = tmp_path / "visual.pdf"
    _text_pdf(visual, visual=True)
    other = tmp_path / "other"
    init_project(other, "测试论文", "en", "zh-CN")
    import_paper(other, visual)
    manifest_path = other / "source" / "pdf" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "complete"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeError, match="PDF_QA_MANIFEST_MISMATCH"):
        run(["pdf-validate", str(other)])


def test_pdf_ledgers_are_digest_pinned_and_semantically_validated(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    _text_pdf(source)
    project = _new_project(tmp_path)
    import_paper(project, source)
    manifest_path = project / "source" / "pdf" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert set(manifest["artifacts"]) == {
        "article_map",
        "asset_manifest",
        "backend",
        "base_blocks",
        "base_relations",
        "object_accounting",
        "qa",
        "raw_objects",
        "render_tree",
    }
    raw_path = project / "source" / manifest["artifacts"]["raw_objects"]["path"]
    raw_rows = [
        json.loads(line) for line in raw_path.read_text(encoding="utf-8").splitlines()
    ]
    raw_rows[0]["payload"] += " tampered"
    raw_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in raw_rows),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="PDF_ARTIFACT_DIGEST_MISMATCH"):
        import_paper(project, source)

    other = tmp_path / "other"
    init_project(other, "测试论文", "en", "zh-CN")
    import_paper(other, source)
    other_manifest_path = other / "source" / "pdf" / "manifest.json"
    other_manifest = json.loads(other_manifest_path.read_text(encoding="utf-8"))
    blocks_path = other / "source" / other_manifest["artifacts"]["base_blocks"]["path"]
    blocks = [json.loads(line) for line in blocks_path.read_text(encoding="utf-8").splitlines()]
    blocks[0]["ordinal"] = 2
    blocks_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in blocks),
        encoding="utf-8",
    )
    other_manifest["artifacts"]["base_blocks"]["sha256"] = hashlib.sha256(
        blocks_path.read_bytes()
    ).hexdigest()
    other_manifest_path.write_text(
        json.dumps(other_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="PDF_BLOCK_LEDGER_INVALID"):
        import_paper(other, source)


def test_caption_relations_and_primary_object_ownership_are_explicit(tmp_path: Path) -> None:
    source = tmp_path / "figures.pdf"
    _two_figure_pdf(source)
    project = _new_project(tmp_path)
    import_paper(project, source)
    manifest_path = project / "source" / "pdf" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    run_root = project / "source" / "pdf" / "runs" / manifest["active_run_id"]
    relations = [
        json.loads(line)
        for line in (run_root / "base-relations.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(relations) == 2
    assert {item["type"] for item in relations} == {"caption_of"}

    accounting_path = run_root / "object-accounting.jsonl"
    accounting = [
        json.loads(line) for line in accounting_path.read_text(encoding="utf-8").splitlines()
    ]
    owned = next(item for item in accounting if item["primary_block_id"] is not None)
    blocks = _pdf_blocks(project)
    wrong_owner = next(
        item["block_id"]
        for item in blocks
        if item["block_id"] != owned["primary_block_id"]
        and owned["object_ref"] not in item["source_object_refs"]
    )
    owned["primary_block_id"] = wrong_owner
    accounting_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in accounting),
        encoding="utf-8",
    )
    manifest["artifacts"]["object_accounting"]["sha256"] = hashlib.sha256(
        accounting_path.read_bytes()
    ).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="PDF_OBJECT_ACCOUNTING_INVALID"):
        import_paper(project, source)


def test_pdf_reimport_validates_preserved_original(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    _text_pdf(source)
    project = _new_project(tmp_path)
    import_paper(project, source)
    (project / "source" / "original.pdf").write_bytes(b"%PDF-broken")
    with pytest.raises(RuntimeError, match="PDF_SOURCE_DIGEST_MISMATCH"):
        import_paper(project, source)


def test_three_column_and_rotated_pages_never_complete(tmp_path: Path) -> None:
    columns = tmp_path / "three.pdf"
    _three_column_pdf(columns)
    project = _new_project(tmp_path)
    import_paper(project, columns)
    qa = json.loads((project / "source" / "pdf" / "qa.json").read_text())
    assert qa["status"] in {"incomplete", "unsupported"}
    assert qa["metrics"]["ambiguous_layout_pages"] == [1]
    assert any(item["code"] == "PDF_LAYOUT_AMBIGUOUS" for item in qa["issues"])

    for angle in (90, 180, 270):
        rotated = tmp_path / f"rotated-{angle}.pdf"
        document = canvas.Canvas(str(rotated), pagesize=A4, invariant=True)
        document.setPageRotation(angle)
        document.setFont("Helvetica-Bold", 17)
        document.drawString(60, 500, "Rotated Source Page")
        document.setFont("Helvetica", 10)
        document.drawString(60, 470, "Rotated text remains reviewable but cannot pass P1.")
        document.save()
        other = tmp_path / f"rotated-project-{angle}"
        init_project(other, "旋转页面", "en", "zh-CN")
        import_paper(other, rotated)
        rotated_qa = json.loads((other / "source" / "pdf" / "qa.json").read_text())
        assert rotated_qa["status"] in {"incomplete", "unsupported"}
        assert rotated_qa["metrics"]["rotated_pages"] == [1]
        assert any(
            item["code"] == "PDF_ROTATED_PAGE_UNRESOLVED"
            for item in rotated_qa["issues"]
        )
        assert rotated_qa["ocr_candidates"]["pages"] == []


def test_interrupted_pdf_commit_is_resumable(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "paper.pdf"
    _text_pdf(source)
    project = _new_project(tmp_path)
    original_write = pdf_import_module.atomic_write_bytes
    destination_writes = 0

    def fail_during_commit(path: Path, value: bytes) -> None:
        nonlocal destination_writes
        if path.is_relative_to(project / "source"):
            destination_writes += 1
            if destination_writes == 3:
                raise OSError("injected commit interruption")
        original_write(path, value)

    monkeypatch.setattr(pdf_import_module, "atomic_write_bytes", fail_during_commit)
    with pytest.raises(OSError, match="injected"):
        import_paper(project, source)
    assert (project / "source" / "pdf-import.pending.json").exists()
    assert not (project / "source" / "source.json").exists()

    monkeypatch.setattr(pdf_import_module, "atomic_write_bytes", original_write)
    imported = import_paper(project, source)
    assert imported.format == "pdf"
    assert not (project / "source" / "pdf-import.pending.json").exists()


def test_unnumbered_heading_is_a_section_and_unknown_bold_is_blocked(tmp_path: Path) -> None:
    source = tmp_path / "heading.pdf"
    document = canvas.Canvas(str(source), pagesize=A4, invariant=True)
    _, height = A4
    document.setFont("Helvetica-Bold", 17)
    document.drawString(42, height - 60, "Unnumbered Sections")
    document.setFont("Helvetica-Bold", 12)
    document.drawString(42, height - 110, "Introduction")
    document.setFont("Helvetica", 10)
    document.drawString(
        42,
        height - 135,
        "This section has enough deterministic source text to remain independently reviewable.",
    )
    document.drawString(
        42,
        height - 153,
        "Its paragraph is associated with the unnumbered heading rather than the document title.",
    )
    document.save()
    project = _new_project(tmp_path)
    import_paper(project, source)
    article = (project / "source" / "article.md").read_text(encoding="utf-8")
    assert "## Introduction" in article
    passages, _ = segment_paper(project)
    assert {item.section_title for item in passages} == {"Introduction"}


def test_pdf_policy_mismatch_and_invalid_policy_are_explicit(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    _text_pdf(source)
    project = _new_project(tmp_path)
    import_paper(project, source)
    changed = default_policy()
    changed["line_split_gap_points"] = 30.0
    changed_path = tmp_path / "changed-policy.json"
    changed_path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="PDF_POLICY_MISMATCH"):
        import_paper(project, source, pdf_policy=changed_path)

    unsafe = default_policy()
    unsafe["render_scale"] = 100.0
    unsafe_path = tmp_path / "unsafe-policy.json"
    unsafe_path.write_text(json.dumps(unsafe), encoding="utf-8")
    fresh = tmp_path / "fresh"
    init_project(fresh, "策略测试", "en", "zh-CN")
    with pytest.raises(ValueError, match="PDF_POLICY_INVALID"):
        import_paper(fresh, source, pdf_policy=unsafe_path)
    assert not (fresh / "source" / "source.json").exists()

    weakened = default_policy()
    weakened["min_visible_ink_accounting_ratio"] = 0.99
    weakened_path = tmp_path / "weakened-policy.json"
    weakened_path.write_text(json.dumps(weakened), encoding="utf-8")
    with pytest.raises(ValueError, match="weakens a hard invariant"):
        import_paper(fresh, source, pdf_policy=weakened_path)


def test_corrupt_pdf_has_stable_error_and_commits_nothing(tmp_path: Path) -> None:
    source = tmp_path / "broken.pdf"
    source.write_bytes(b"%PDF-1.7\ngarbage")
    project = _new_project(tmp_path)
    with pytest.raises(ValueError, match="PDF_CORRUPT"):
        import_paper(project, source)
    assert not (project / "source" / "source.json").exists()


def test_cropbox_coordinates_match_rendered_page(tmp_path: Path) -> None:
    source = tmp_path / "cropped.pdf"
    uncropped = tmp_path / "uncropped.pdf"
    document = canvas.Canvas(str(uncropped), pagesize=A4, invariant=True)
    document.setFont("Helvetica-Bold", 17)
    document.drawString(42, 610, "Cropped Page Study")
    document.setFont("Helvetica-Bold", 11)
    document.drawString(42, 570, "Introduction")
    document.setFont("Helvetica", 10)
    for index, text in enumerate(
        (
            "CropBox coordinates must align text blocks with the rendered page evidence.",
            "The canonical page size follows the visible crop rather than the larger MediaBox.",
            "Every passage retains a page and bounding box after deterministic extraction.",
        )
    ):
        document.drawString(54, 545 - index * 18, text)
    document.save()
    reader = PdfReader(uncropped)
    reader.pages[0].cropbox.lower_left = (0, 0)
    reader.pages[0].cropbox.upper_right = (480, 670)
    writer = PdfWriter()
    writer.add_page(reader.pages[0])
    with source.open("wb") as handle:
        writer.write(handle)
    project = _new_project(tmp_path)
    import_paper(project, source)
    manifest = json.loads((project / "source" / "pdf" / "manifest.json").read_text())
    run_root = project / "source" / "pdf" / "runs" / manifest["active_run_id"]
    first_block = json.loads((run_root / "base-blocks.jsonl").read_text().splitlines()[0])
    provenance = first_block["provenance"][0]
    assert provenance["page_width"] == pytest.approx(480)
    assert provenance["page_height"] == pytest.approx(670)
    assert provenance["crop_box"] == pytest.approx([0, A4[1] - 670, 480, A4[1]])


def test_dehyphenation_and_cross_page_continuation_are_audited(tmp_path: Path) -> None:
    source = tmp_path / "continuation.pdf"
    document = canvas.Canvas(str(source), pagesize=A4, invariant=True)
    _, height = A4
    document.setFont("Helvetica-Bold", 17)
    document.drawString(42, height - 60, "Continuation Audit")
    document.setFont("Helvetica-Bold", 11)
    document.drawString(42, height - 100, "Introduction")
    document.setFont("Helvetica", 10)
    document.drawString(42, height - 130, "A same-page phrase ends with determin-")
    document.drawString(42, height - 145, "istic text that cannot be silently normalized.")
    document.drawString(42, 55, "The final page line continues with environ-")
    document.showPage()
    document.setFont("Helvetica", 10)
    document.drawString(42, height - 55, "mental evidence on the following page.")
    document.drawString(42, height - 75, "The remainder closes with a complete sentence.")
    document.save()
    project = _new_project(tmp_path)
    import_paper(project, source)
    qa = json.loads((project / "source" / "pdf" / "qa.json").read_text())
    codes = {item["code"] for item in qa["issues"]}
    assert "PDF_DEHYPHENATION_AMBIGUOUS" in codes
    assert "PDF_CROSS_PAGE_CONTINUATION_UNRESOLVED" in codes
    manifest = json.loads((project / "source" / "pdf" / "manifest.json").read_text())
    run_root = project / "source" / "pdf" / "runs" / manifest["active_run_id"]
    blocks = [json.loads(line) for line in (run_root / "base-blocks.jsonl").read_text().splitlines()]
    audited = [item for item in blocks if "PDF_DEHYPHENATION_AMBIGUOUS" in item["issues"]]
    assert audited and audited[0]["raw_text"] != audited[0]["text"]
    assert audited[0]["transformations"]


def test_geometry_proven_cross_page_paragraph_is_merged(tmp_path: Path) -> None:
    source = tmp_path / "certain-continuation.pdf"
    document = canvas.Canvas(str(source), pagesize=A4, invariant=True)
    _, height = A4
    document.setFont("Helvetica-Bold", 17)
    document.drawString(42, height - 60, "Certain Continuation")
    document.setFont("Helvetica-Bold", 11)
    document.drawString(42, height - 100, "Introduction")
    document.setFont("Helvetica", 10)
    document.drawString(42, height - 130, "A complete opening paragraph establishes context.")
    document.drawString(42, 55, "This deterministic paragraph continues with")
    document.showPage()
    document.setFont("Helvetica", 10)
    document.drawString(42, height - 55, "lowercase source text and ends on the next page.")
    document.drawString(42, height - 80, "A separate paragraph follows with a complete sentence.")
    document.save()
    project = _new_project(tmp_path)
    import_paper(project, source)
    blocks = _pdf_blocks(project)
    merged = next(
        item for item in blocks if "continues with lowercase source text" in (item["text"] or "")
    )
    assert [item["page"] for item in merged["provenance"]] == [1, 2]
    assert any(
        item["kind"] == "join_cross_page" for item in merged["transformations"]
    )
    assert "PDF_CROSS_PAGE_CONTINUATION_UNRESOLVED" not in merged["issues"]


def test_hairline_rule_is_excluded_artifact_not_visual_content(tmp_path: Path) -> None:
    source = tmp_path / "thin-rule.pdf"
    document = canvas.Canvas(str(source), pagesize=A4, invariant=True)
    _, height = A4
    document.setFont("Helvetica-Bold", 17)
    document.drawString(42, height - 72, "Rule Separator Study")
    document.setFont("Helvetica-Bold", 11)
    document.drawString(42, height - 112, "Abstract")
    document.setFont("Helvetica", 10)
    document.drawString(
        42,
        height - 135,
        "A thin decorative rule below must not be treated as recoverable visual content.",
    )
    document.setLineWidth(0.5)
    document.line(42, height - 155, 400, height - 155)
    document.setFont("Helvetica", 10)
    document.drawString(
        42,
        height - 175,
        "The page otherwise contains only selectable text and completes cleanly.",
    )
    document.save()
    project = _new_project(tmp_path)
    import_paper(project, source)
    qa = json.loads((project / "source" / "pdf" / "qa.json").read_text(encoding="utf-8"))
    assert qa["status"] in {"complete", "complete_with_warnings"}
    assert not any(
        item["code"] == "PDF_VISIBLE_REGION_UNRESOLVED" for item in qa["issues"]
    )


def test_corroborated_dehyphenation_joins_and_resolves(tmp_path: Path) -> None:
    source = tmp_path / "dehyphen.pdf"
    document = canvas.Canvas(str(source), pagesize=A4, invariant=True)
    _, height = A4
    document.setFont("Helvetica-Bold", 17)
    document.drawString(42, height - 72, "Dehyphenation Evidence")
    document.setFont("Helvetica-Bold", 11)
    document.drawString(42, height - 112, "Introduction")
    document.setFont("Helvetica", 10)
    document.drawString(
        42,
        height - 135,
        "This study covers environmental enforcement that splits environ-",
    )
    document.drawString(
        42, height - 143, "mental compliance across monitoring stations."
    )
    document.drawString(
        42,
        height - 170,
        "The independent environmental policy applies to rural counties.",
    )
    document.save()
    project = _new_project(tmp_path)
    import_paper(project, source)
    qa = json.loads((project / "source" / "pdf" / "qa.json").read_text(encoding="utf-8"))
    assert not any(
        item["code"] == "PDF_DEHYPHENATION_AMBIGUOUS" for item in qa["issues"]
    )
    manifest = json.loads(
        (project / "source" / "pdf" / "manifest.json").read_text(encoding="utf-8")
    )
    run_root = project / "source" / "pdf" / "runs" / manifest["active_run_id"]
    blocks = [
        json.loads(line)
        for line in (run_root / "base-blocks.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    paragraph = next(
        item for item in blocks if "environmental compliance" in item["text"]
    )
    assert "environ-mental" not in paragraph["text"]
    assert "environmental compliance" in paragraph["text"]


def test_compound_hyphen_preserves_hyphen_and_blocks(tmp_path: Path) -> None:
    source = tmp_path / "compound.pdf"
    document = canvas.Canvas(str(source), pagesize=A4, invariant=True)
    _, height = A4
    document.setFont("Helvetica-Bold", 17)
    document.drawString(42, height - 72, "Compound Hyphen Study")
    document.setFont("Helvetica-Bold", 11)
    document.drawString(42, height - 112, "Introduction")
    document.setFont("Helvetica", 10)
    document.drawString(
        42,
        height - 135,
        "We estimate the fixed-",
    )
    document.drawString(
        42, height - 143, "effect regression on the full panel."
    )
    document.drawString(
        42,
        height - 170,
        "A separate fixed effect specification confirms the pattern.",
    )
    document.save()
    project = _new_project(tmp_path)
    import_paper(project, source)
    qa = json.loads((project / "source" / "pdf" / "qa.json").read_text(encoding="utf-8"))
    assert any(item["code"] == "PDF_DEHYPHENATION_AMBIGUOUS" for item in qa["issues"])
    manifest = json.loads(
        (project / "source" / "pdf" / "manifest.json").read_text(encoding="utf-8")
    )
    run_root = project / "source" / "pdf" / "runs" / manifest["active_run_id"]
    blocks = [
        json.loads(line)
        for line in (run_root / "base-blocks.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    paragraph = next(
        item
        for item in blocks
        if "PDF_DEHYPHENATION_AMBIGUOUS" in item["issues"]
        and "fixed" in item["text"]
    )
    assert "fixed-effect" in paragraph["text"]


def test_visible_ink_components_are_not_hidden_by_a_union_box() -> None:
    ink = Image.new("L", (120, 30), 0)
    overlap = Image.new("L", ink.size, 0)
    ink_draw = ImageDraw.Draw(ink)
    overlap_draw = ImageDraw.Draw(overlap)
    for box in ((5, 5, 25, 25), (50, 5, 70, 25), (95, 5, 115, 25)):
        ink_draw.rectangle(box, fill=255)
    overlap_draw.rectangle((5, 5, 25, 25), fill=255)
    overlap_draw.rectangle((95, 5, 115, 25), fill=255)
    accounted, total = pdf_import_module._component_accounting(ink, overlap)
    assert 0 < accounted < total


def _narrow_char(**kwargs: object) -> dict[str, object]:
    row: dict[str, object] = {
        "text": "(cid:0)",
        "fontname": "KFCUEC+TeX_CM_Maths_Symbols",
        "width": 1.99,
        "size": 7.97,
    }
    row.update(kwargs)
    return row


def test_tex_math_sybol_minus_is_recovered() -> None:
    # Computer Modern maths-symbols subset exposes an unmapped cid:0 glyph that is
    # the U+2212 minus. The backend must recover it, once, with corroborating geometry.
    assert _recover_unmapped_glyph(_narrow_char()) == "−"
    # A different subset prefix must still strip and recover.
    assert (
        _recover_unmapped_glyph(_narrow_char(fontname="ABCDEF+TeX_CM_Maths_Symbols"))
        == "−"
    )


def test_tex_math_glyph_is_not_recovered_without_corroboration() -> None:
    # Ordinary text, a non-math font, and a wide (non-minus) glyph all stay unmapped.
    assert _recover_unmapped_glyph(_narrow_char(text="A")) == "A"
    assert _recover_unmapped_glyph(_narrow_char(fontname="ABCDEF+TimesNewRoman")) == "(cid:0)"
    assert _recover_unmapped_glyph(_narrow_char(width=8.0, size=8.0)) == "(cid:0)"  # a box, not a minus


def _pdf_blocks(project: Path) -> list[dict]:
    manifest = json.loads(
        (project / "source" / "pdf" / "manifest.json").read_text(encoding="utf-8")
    )
    run_root = project / "source" / "pdf" / "runs" / manifest["active_run_id"]
    return [
        json.loads(line)
        for line in (run_root / "base-blocks.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_nearby_unboxed_table_cannot_steal_captioned_chart_evidence(
    tmp_path: Path,
) -> None:
    source = tmp_path / "table-and-chart.pdf"
    _nearby_unboxed_table_and_chart_pdf(source)
    project = _new_project(tmp_path)
    import_paper(project, source)
    blocks = _pdf_blocks(project)
    table = next(block for block in blocks if block["kind"] == "table")
    figure = next(block for block in blocks if block["kind"] == "figure")
    assert table["status"] == "unresolved"
    assert figure["status"] == "ok"
    assert set(table["source_object_refs"]).isdisjoint(figure["source_object_refs"])
    manifest = json.loads(
        (project / "source" / "pdf" / "manifest.json").read_text(encoding="utf-8")
    )
    run_root = project / "source" / "pdf" / "runs" / manifest["active_run_id"]
    raw = [
        json.loads(line)
        for line in (run_root / "raw-objects.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    accounting = [
        json.loads(line)
        for line in (run_root / "object-accounting.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert {item["object_ref"] for item in raw} == {
        item["object_ref"] for item in accounting
    }
    by_ref = {item["object_ref"]: item for item in accounting}
    chart_marker = [item["object_ref"] for item in raw if item["payload"] == "Z"]
    assert chart_marker
    assert all(by_ref[ref]["primary_block_id"] == figure["block_id"] for ref in chart_marker)


def test_boxed_table_verifies_pipe_render_and_accounts(tmp_path: Path) -> None:
    source = tmp_path / "table.pdf"
    _table_paper_pdf(source, boxed=True)
    project = _new_project(tmp_path)
    import_paper(project, source)
    blocks = _pdf_blocks(project)
    table = next((block for block in blocks if block["kind"] == "table"), None)
    assert table is not None
    assert table["status"] == "ok"
    assert table["table"]["structure_verified"] is True
    assert table["table"]["rows"] == [["Group", "Mean", "SD"], ["A", "4.2", "0.3"], ["B", "5.1", "0.4"]]
    assert table["table"]["header_rows"] == 1
    article = _table_article(project)
    assert "| Group | Mean | SD |" in article
    assert "| --- | --- | --- |" in article
    assert "| A | 4.2 | 0.3 |" in article
    tree = json.loads(
        (project / "source" / "pdf" / "render-tree.json").read_text(encoding="utf-8")
    )
    table_index = next(
        index for index, node in enumerate(tree["nodes"]) if node["type"] == "table"
    )
    assert tree["nodes"][table_index - 1]["slots"][0]["source_text"].startswith(
        "Table 1"
    )
    qa = json.loads((project / "source" / "pdf" / "qa.json").read_text(encoding="utf-8"))
    assert qa["status"] == "complete"
    assert qa["metrics"]["verified_tables"] == 1
    assert qa["metrics"]["source_object_accounting_ratio"] == 1.0


def test_boxed_table_cells_become_passages_and_round_trip_translation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "table.pdf"
    _table_paper_pdf(source, boxed=True)
    project = _new_project(tmp_path)
    import_paper(project, source)
    passages, _ = segment_paper(project, unit_size=1)
    passage_text = {item.text for item in passages}
    assert "Group" in passage_text
    assert "Mean" in passage_text
    assert "SD" not in passage_text
    assert "A" not in passage_text
    assert "4.2" not in passage_text
    assert not any("| Group |" in item.text for item in passages)

    slots = [
        json.loads(line)
        for line in (project / "state" / "passage-slots.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    table_slots = [item for item in slots if item["role"] == "table_cell"]
    assert {tuple(item["sub_locator"].values()) for item in table_slots} == {
        (0, 0),
        (0, 1),
    }
    provenance = [
        json.loads(line)
        for line in (project / "state" / "passage-provenance.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert any(item["sub_locator"] == {"row": 0, "column": 0} for item in provenance)
    slots_before = (project / "state" / "passage-slots.jsonl").read_bytes()
    passages_before = (project / "state" / "passages.jsonl").read_bytes()
    segment_paper(project, unit_size=1)
    assert (project / "state" / "passage-slots.jsonl").read_bytes() == slots_before
    assert (project / "state" / "passages.jsonl").read_bytes() == passages_before

    translate_paper(project, MockTranslationAdapter())
    exported = export_translated_markdown(project).read_text(encoding="utf-8")
    assert "| [MOCK zh-CN] Group | [MOCK zh-CN] Mean | SD |" in exported
    assert "| A | 4.2 | 0.3 |" in exported
    assert "| B | 5.1 | 0.4 |" in exported
    assert "paperweaver:block" not in exported
    pdf = render_translation_pdf(project / "output" / "translated.md")
    assert pdf.read_bytes().startswith(b"%PDF")
    rendered_text = " ".join(page.extract_text() or "" for page in PdfReader(pdf).pages)
    assert "Group" in rendered_text and "4.2" in rendered_text


def test_table_rendering_escapes_structure_and_preserves_line_breaks() -> None:
    pipe = _pipe_table([["Header|A", "<script>"], ["line 1\nline 2", "4.2"]], 1)
    assert pipe[0] == r"| Header\|A | &lt;script&gt; |"
    assert "line 1<br>line 2" in pipe[2]
    html_rows = _html_table([["Header", "<script>"], ["line 1\nline 2", "4.2"]], 1, [], [])
    rendered = "\n".join(html_rows)
    assert "&lt;script&gt;" in rendered
    assert "line 1<br>line 2" in rendered


def test_visual_clusters_bind_to_each_caption_without_chart_text_passages(
    tmp_path: Path,
) -> None:
    source = tmp_path / "figures.pdf"
    _two_figure_pdf(source)
    project = _new_project(tmp_path)
    import_paper(project, source)
    blocks = _pdf_blocks(project)
    figures = [item for item in blocks if item["kind"] == "figure"]
    assert len(figures) == 2
    assert all(item["status"] == "ok" and item["asset_refs"] for item in figures)
    assert figures[0]["provenance"][0]["bbox"][3] < figures[1]["provenance"][0]["bbox"][1]
    assert not any(
        item["kind"] == "unknown" and item["status"] == "unresolved"
        for item in blocks
    )
    passages, _ = segment_paper(project)
    assert not any("Axis label" in item.text for item in passages)
    assert {item.text for item in passages if item.text.startswith("Fig.")} == {
        "Fig. 1. Deterministic panel 1.",
        "Fig. 2. Deterministic panel 2.",
    }
    translate_paper(project, MockTranslationAdapter())
    translated = export_translated_markdown(project).read_text(encoding="utf-8")
    assert translated.index("![Fig. 1]") < translated.index(
        "[MOCK zh-CN] Fig. 1. Deterministic panel 1."
    )
    assert translated.index("![Fig. 2]") < translated.index(
        "[MOCK zh-CN] Fig. 2. Deterministic panel 2."
    )
    assert len(list((project / "output" / "assets").glob("sha256-*.png"))) == 2


def test_image_only_page_is_an_ocr_candidate_without_running_ocr(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "scan.png"
    image = Image.new("RGB", (800, 1000), "white")
    draw = ImageDraw.Draw(image)
    for row in range(20):
        draw.rectangle((80, 80 + row * 40, 720, 96 + row * 40), fill="black")
    image.save(image_path)
    source = tmp_path / "scan.pdf"
    document = canvas.Canvas(str(source), pagesize=A4, invariant=True)
    document.drawImage(str(image_path), 0, 0, width=A4[0], height=A4[1])
    document.save()
    project = _new_project(tmp_path)
    import_paper(project, source)
    qa = json.loads((project / "source" / "pdf" / "qa.json").read_text())
    assert qa["status"] == "unsupported"
    assert qa["ocr_candidates"]["pages"] == [
        {
            "page": 1,
            "reason": "native_text_below_100_valid_characters",
            "valid_characters": 0,
            "invalid_characters": 0,
        }
    ]
    assert not (project / "source" / "pdf").joinpath("ocr-runs").exists()


def test_text_layer_equation_is_verified_numbered_cropped_and_literal(
    tmp_path: Path,
) -> None:
    source = tmp_path / "equation.pdf"
    _equation_pdf(source)
    project = _new_project(tmp_path)
    import_paper(project, source)
    blocks = _pdf_blocks(project)
    equations = [item for item in blocks if item["kind"] == "equation"]
    assert len(equations) == 1
    equation = equations[0]
    assert equation["status"] == "ok"
    assert equation["equation"] == {
        "latex": "y_{it} = b_{1} x_{it} + e_{it}",
        "number": "1",
        "latex_verified": True,
    }
    assert equation["asset_refs"]
    asset = next(
        json.loads(line)
        for line in (project / "source" / "assets" / "manifest.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if json.loads(line)["asset_id"] == equation["asset_refs"][0]
    )
    assert asset["bbox"][3] == pytest.approx(
        equation["provenance"][0]["bbox"][3] + 3.0
    )
    article = (project / "source" / "article.md").read_text(encoding="utf-8")
    assert "y_{it} = b_{1} x_{it} + e_{it} \\tag{1}" in article
    qa = json.loads((project / "source" / "pdf" / "qa.json").read_text())
    assert qa["metrics"]["equations"] == 1
    assert qa["metrics"]["verified_equations"] == 1
    passages, _ = segment_paper(project)
    assert not any("y_{it}" in item.text or "tag{1}" in item.text for item in passages)
    translate_paper(project, MockTranslationAdapter())
    translated = export_translated_markdown(project)
    translated_text = translated.read_text(encoding="utf-8")
    assert "y_{it} = b_{1} x_{it} + e_{it} \\tag{1}" in translated_text
    rendered = render_translation_pdf(translated)
    assert rendered.read_bytes().startswith(b"%PDF")


def test_text_layer_equation_rejects_unsupported_tex_specials(tmp_path: Path) -> None:
    source = tmp_path / "unsafe-equation.pdf"
    _equation_pdf(source, unsafe_tex=True)
    project = _new_project(tmp_path)
    import_paper(project, source)
    equation = next(item for item in _pdf_blocks(project) if item["kind"] == "equation")
    assert equation["status"] == "unresolved"
    assert equation["equation"]["latex"] is None
    assert equation["equation"]["latex_verified"] is False


def test_unboxed_table_is_honest_fallback(tmp_path: Path) -> None:
    source = tmp_path / "unboxed.pdf"
    _table_paper_pdf(source, boxed=False)
    project = _new_project(tmp_path)
    import_paper(project, source)
    blocks = _pdf_blocks(project)
    assert not any(block["kind"] == "table" and block["status"] == "ok" for block in blocks)
    caption = next(block for block in blocks if block["kind"] == "table_caption")
    assert caption["status"] == "unresolved"
    assert "PDF_TABLE_UNRESOLVED" in caption["issues"]
    qa = json.loads((project / "source" / "pdf" / "qa.json").read_text(encoding="utf-8"))
    assert qa["status"] == "incomplete"
    assert qa["metrics"]["verified_tables"] == 0


def test_partial_rule_table_is_honest_fallback(tmp_path: Path) -> None:
    source = tmp_path / "partial.pdf"
    _table_paper_pdf(source, boxed=True, partial_vertical=True)
    project = _new_project(tmp_path)
    import_paper(project, source)
    blocks = _pdf_blocks(project)
    assert not any(block["kind"] == "table" and block["status"] == "ok" for block in blocks)
    qa = json.loads((project / "source" / "pdf" / "qa.json").read_text(encoding="utf-8"))
    assert qa["metrics"]["verified_tables"] == 0
    assert any(
        "PDF_TABLE_UNRESOLVED" in block["issues"] for block in blocks if block["kind"] in {"table", "table_caption"}
    )
