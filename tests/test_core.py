import json
from pathlib import Path

import pytest

import paperweaver.translation as translation_module
from paperweaver.cli import run
from paperweaver.core import import_paper, init_project, parse_sections
from paperweaver.publication import SONGTI_NAME, _register_chinese_font, render_translation_pdf
from paperweaver.summary import export_chinese_summary, import_chinese_summary
from paperweaver.translation import (
    MockTranslationAdapter,
    _separate_display_formula,
    export_translated_markdown,
    import_translation_draft,
    segment_paper,
    translate_paper,
    validate_translations,
)


def _project(tmp_path: Path) -> Path:
    source = tmp_path / "paper.md"
    source.write_text(
        "# A study\n\n## Methods\n\nMethod text.\n\n## Results\n\nResult text.\n",
        encoding="utf-8",
    )
    project = tmp_path / "project"
    init_project(project, "一项研究", "en", "zh-CN")
    import_paper(project, source)
    segment_paper(project, unit_size=1)
    return project


def test_import_preserves_source_and_numbered_text_headings(tmp_path: Path) -> None:
    source = tmp_path / "paper.txt"
    source.write_text("TITLE: Actual title\n\n1 Introduction\n\nQuestion.\n", encoding="utf-8")
    project = tmp_path / "project"
    init_project(project, "Fallback", "en", "zh-CN")
    imported = import_paper(project, source)
    assert imported.title == "Actual title"
    normalised = (project / "source" / "article.md").read_text(encoding="utf-8")
    assert "## 1 Introduction" in normalised
    assert parse_sections(normalised)[-1].title == "1 Introduction"
    replacement = tmp_path / "replacement.md"
    replacement.write_text("# Replacement\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        import_paper(project, replacement)


def test_completed_translation_exports_markdown_and_pdf(tmp_path: Path) -> None:
    project = _project(tmp_path)
    assert translate_paper(project, MockTranslationAdapter()) == (2, 0)
    assert validate_translations(project) == []
    markdown = export_translated_markdown(project)
    assert markdown.name == "translated.md"
    assert "> Method text." not in markdown.read_text(encoding="utf-8")
    assert "[MOCK zh-CN] Method text." in markdown.read_text(encoding="utf-8")
    assert "## Methods" not in markdown.read_text(encoding="utf-8")
    pdf = render_translation_pdf(markdown)
    assert pdf.exists() and pdf.read_bytes().startswith(b"%PDF")
    assert _register_chinese_font() == SONGTI_NAME


def test_translation_export_rejects_incomplete_coverage(tmp_path: Path) -> None:
    project = _project(tmp_path)
    with pytest.raises(RuntimeError, match="incomplete"):
        export_translated_markdown(project)


def test_terminal_numbered_formula_is_rendered_as_a_display_block() -> None:
    assert _separate_display_formula("具体公式如下：lnEPit=η0+η1DEit（1）") == [
        "具体公式如下：", "", "$$ lnEPit=η0+η1DEit（1） $$", ""
    ]


def test_jats_export_retains_authors_and_original_language_references(tmp_path: Path) -> None:
    source = tmp_path / "paper.xml"
    source.write_text(
        """<article><front><article-meta><title-group><article-title>Study</article-title></title-group>
        <contrib-group><contrib contrib-type="author"><name><surname>Yuan</surname><given-names>Honglin</given-names></name></contrib></contrib-group>
        <aff>Example University, China</aff></article-meta></front><body><sec><title>Methods</title><p>Method text.</p></sec></body>
        <back><ref-list><ref><label>1</label><mixed-citation>Smith J. (2024). Example reference.</mixed-citation></ref></ref-list></back></article>""",
        encoding="utf-8",
    )
    project = tmp_path / "project"
    init_project(project, "研究", "en", "zh-CN")
    import_paper(project, source)
    segment_paper(project)
    translate_paper(project, MockTranslationAdapter())
    exported = export_translated_markdown(project).read_text(encoding="utf-8")
    assert "## 作者" in exported and "Honglin Yuan" in exported
    assert "## 作者单位" in exported and "Example University, China" in exported
    assert "## 参考文献" in exported and "Smith J. (2024). Example reference." in exported


def test_jats_visuals_are_detected_from_source_markers_not_caption_style(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "paper.xml"
    source.write_text(
        """<article><front><article-meta><title-group><article-title>Study</article-title></title-group></article-meta></front>
        <body><sec><title>Methods</title><p>Method text.</p><fig><object-id>10.1371/example.g001</object-id><label>Fig 1</label><caption><title>Original caption.</title></caption><graphic/></fig></sec></body></article>""",
        encoding="utf-8",
    )
    project = tmp_path / "project"
    init_project(project, "研究", "en", "zh-CN")
    import_paper(project, source)
    segment_paper(project)
    translate_paper(project, MockTranslationAdapter())

    def fake_download(doi: str, destination: Path) -> None:
        assert doi == "10.1371/example.g001"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"png")

    monkeypatch.setattr(translation_module, "_download_plos_visual", fake_download)
    exported = export_translated_markdown(project).read_text(encoding="utf-8")
    assert "![图：图1" in exported
    assert "assets/figure-1.png" in exported


def test_translation_import_appends_a_revision(tmp_path: Path) -> None:
    project = _project(tmp_path)
    passages, _ = segment_paper(project, unit_size=1)
    draft = tmp_path / "translation.jsonl"
    draft.write_text(
        '{"passage_id": "' + passages[0].id + '", "translated_text": "初译"}\n', encoding="utf-8"
    )
    assert import_translation_draft(project, draft, "agent", "test", "initial") == 1
    draft.write_text(
        '{"passage_id": "' + passages[0].id + '", "translated_text": "修订译文"}\n', encoding="utf-8"
    )
    assert import_translation_draft(project, draft, "agent", "test", "revision") == 1


def test_translation_import_is_atomic_and_rejects_duplicate_passages(tmp_path: Path) -> None:
    project = _project(tmp_path)
    passages, _ = segment_paper(project, unit_size=1)
    translations = project / "state" / "translations.jsonl"
    before = translations.read_bytes()
    draft = tmp_path / "broken-translation.jsonl"
    draft.write_text(
        json.dumps({"passage_id": passages[0].id, "translated_text": "初译"})
        + "\n{not json}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid JSON"):
        import_translation_draft(project, draft, "agent", "test", "initial")
    assert translations.read_bytes() == before

    row = json.dumps({"passage_id": passages[0].id, "translated_text": "初译"})
    draft.write_text(row + "\n" + row + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate passage"):
        import_translation_draft(project, draft, "agent", "test", "initial")
    assert translations.read_bytes() == before


def test_translation_revision_chain_tampering_is_rejected(tmp_path: Path) -> None:
    project = _project(tmp_path)
    passages, _ = segment_paper(project, unit_size=1)
    draft = tmp_path / "translation.jsonl"
    draft.write_text(
        json.dumps({"passage_id": passages[0].id, "translated_text": "初译"}) + "\n",
        encoding="utf-8",
    )
    import_translation_draft(project, draft, "agent", "test", "initial")
    path = project / "state" / "translations.jsonl"
    row = json.loads(path.read_text(encoding="utf-8"))
    row["revision"] = 3
    path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="TRANSLATION_LEDGER_INVALID"):
        validate_translations(project)


def test_chinese_summary_requires_evidence_and_exports_four_parts(tmp_path: Path) -> None:
    project = _project(tmp_path)
    passages, _ = segment_paper(project, unit_size=1)
    draft = tmp_path / "summary.json"
    draft.write_text(
        "{" +
        '"overview":"本文考察研究问题。",' +
        '"methods":"作者使用文中报告的方法。",' +
        '"conclusions":"文中报告了相应结果。",' +
        '"limitations":"局限性以原文陈述为准。",' +
        '"evidence_passage_ids":["' + passages[0].id + '"]' +
        "}",
        encoding="utf-8",
    )
    import_chinese_summary(project, draft, "agent", "test")
    output = export_chinese_summary(project)
    text = output.read_text(encoding="utf-8")
    assert all(label in text for label in ("## 全文摘要", "## 方法", "## 结论", "## 局限性"))
    assert passages[0].id in text


def test_cli_exposes_only_delivery_commands(tmp_path: Path) -> None:
    commands = set(run.__globals__["parser"]()._subparsers._group_actions[0].choices)
    assert {"guide", "argument-map", "glossary-import", "entity-import", "export"}.isdisjoint(commands)
    assert {"export-translation", "summary-import", "export-summary"}.issubset(commands)
