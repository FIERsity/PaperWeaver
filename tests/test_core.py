from pathlib import Path

import pytest

from paperweaver.cli import run
from paperweaver.core import build_reading_guide, import_paper, init_project, parse_sections
from paperweaver.models import TranslationRecord
from paperweaver.storage import read_jsonl
from paperweaver.translation import (
    MockTranslationAdapter,
    build_context,
    export_bilingual_markdown,
    import_entities,
    import_glossary,
    import_translation_draft,
    segment_paper,
    translate_paper,
    validate_translations,
)
from paperweaver.understanding import build_argument_map


def test_markdown_structure_and_source_grounded_guide(tmp_path: Path) -> None:
    source = tmp_path / "paper.md"
    source.write_text(
        "# Study\n\n## Abstract\n\nAbstract text.\n\n## Methods\n\nMethod text.\n\n## Results\n\nResult text.\n",
        encoding="utf-8",
    )
    project = tmp_path / "project"
    init_project(project, "Fallback", "en", "zh-CN")
    imported = import_paper(project, source)
    guide = build_reading_guide(project)
    assert imported.title == "Study"
    assert guide.abstract == "Abstract text."
    assert [item.title for item in guide.sections] == ["Study", "Abstract", "Methods", "Results"]
    assert any("方法" in question for question in guide.questions)
    assert (project / "output" / "reading-guide.md").exists()


def test_import_never_replaces_a_different_source(tmp_path: Path) -> None:
    project = tmp_path / "project"
    init_project(project, "Study", "en", "zh-CN")
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("# First\n", encoding="utf-8")
    second.write_text("# Second\n", encoding="utf-8")
    import_paper(project, first)
    with pytest.raises(FileExistsError):
        import_paper(project, second)


def test_plain_text_numbered_sections_become_reviewable_markdown(tmp_path: Path) -> None:
    source = tmp_path / "paper.txt"
    source.write_text("A paper title\n\n1 Introduction\n\nQuestion.\n\n2 Data and methods\n\nMethod.\n", encoding="utf-8")
    project = tmp_path / "project"
    init_project(project, "Fallback", "en", "zh-CN")
    imported = import_paper(project, source)
    assert imported.title == "A paper title"
    normalised = (project / "source" / "article.md").read_text(encoding="utf-8")
    assert "## 1 Introduction" in normalised
    assert "## 2 Data and methods" in normalised
    assert [item.title for item in parse_sections(normalised)] == ["1 Introduction", "2 Data and methods"]


def test_plain_text_title_metadata_is_preferred(tmp_path: Path) -> None:
    source = tmp_path / "paper.txt"
    source.write_text("TITLE: Actual title\n\n1 Introduction\n\nText.\n", encoding="utf-8")
    project = tmp_path / "project"
    init_project(project, "Fallback", "en", "zh-CN")
    assert import_paper(project, source).title == "Actual title"
    assert build_reading_guide(project).title == "Actual title"


def test_cli_workflow(tmp_path: Path) -> None:
    source = tmp_path / "paper.txt"
    source.write_text("# Paper\n\n## Abstract\n\nText.\n", encoding="utf-8")
    project = tmp_path / "project"
    assert run(["init", str(project), "--title", "Paper"]) == 0
    assert run(["import", str(project), str(source)]) == 0
    assert run(["guide", str(project)]) == 0
    assert (project / "output" / "reading-guide.json").exists()


def test_parser_ignores_non_heading_hashes() -> None:
    assert parse_sections("Not a # heading\n\n## Methods\n\nText")[-1].title == "Methods"


def test_jats_import_retains_structure_and_inventory(tmp_path: Path) -> None:
    source = tmp_path / "paper.xml"
    source.write_text(
        """<article><front><article-meta><title-group><article-title>JATS Study</article-title></title-group>
        <abstract><p>Abstract claim.</p></abstract></article-meta></front><body>
        <sec><title>Methods</title><p>Method evidence <xref ref-type=\"bibr\">1</xref>.</p>
        <fig><label>Fig 1</label><caption><p>Figure caption.</p></caption></fig>
        <table-wrap><label>Table 1</label><caption><p>Table caption.</p></caption></table-wrap>
        <disp-formula><mml:math xmlns:mml=\"urn:mml\">x = y</mml:math></disp-formula></sec>
        </body><back><ref-list><ref id=\"r1\"><label>1</label></ref></ref-list></back></article>""",
        encoding="utf-8",
    )
    project = tmp_path / "project"
    init_project(project, "Fallback", "en", "zh-CN")
    imported = import_paper(project, source)
    guide = build_reading_guide(project)
    assert imported.format == "jats"
    assert imported.original_path == "source/original.xml"
    assert guide.inventory is not None
    assert (guide.inventory.figures, guide.inventory.tables, guide.inventory.equations) == (1, 1, 1)
    assert (guide.inventory.citations, guide.inventory.references) == (1, 1)
    normalized = (project / "source" / "article.md").read_text(encoding="utf-8")
    assert "[Figure: Fig 1] Figure caption." in normalized
    assert "[Table: Table 1] Table caption." in normalized
    rendered_guide = (project / "output" / "reading-guide.md").read_text(encoding="utf-8")
    assert "当前版本不下载图像二进制文件" in rendered_guide
    assert "Figure binaries" not in rendered_guide
    passages, _ = segment_paper(project)
    translate_paper(project, MockTranslationAdapter())
    records = read_jsonl(project / "state" / "translations.jsonl", TranslationRecord)
    structural = {item.id for item in passages if item.kind == "structural"}
    assert not structural.intersection({item.passage_id for item in records})


def test_resumable_translation_units_context_revisions_and_bilingual_export(tmp_path: Path) -> None:
    source = tmp_path / "paper.md"
    source.write_text(
        "# Paper\n\n## Introduction\n\nQuestion text.\n\n## Methods\n\nMethod text.\n\n## Results\n\nEvidence text.\n\n## Discussion\n\nBoundary text.\n",
        encoding="utf-8",
    )
    project = tmp_path / "project"
    init_project(project, "Paper", "en", "zh-CN")
    import_paper(project, source)
    passages, units = segment_paper(project, unit_size=1)
    assert len(passages) == 4
    context = build_context(project, units[1])
    assert context.previous_text == "Question text."
    assert context.next_text == "Evidence text."
    assert translate_paper(project, MockTranslationAdapter()) == (4, 0)
    assert translate_paper(project, MockTranslationAdapter()) == (0, 4)
    assert validate_translations(project) == []
    assert "> Question text." in export_bilingual_markdown(project).read_text(encoding="utf-8")

    draft = tmp_path / "revision.jsonl"
    draft.write_text(
        '{"passage_id": "' + passages[0].id + '", "translated_text": "修订译文"}\n',
        encoding="utf-8",
    )
    assert import_translation_draft(project, draft, "agent", "test", "terminology-fix") == 1
    records = read_jsonl(project / "state" / "translations.jsonl", TranslationRecord)
    chain = [item for item in records if item.passage_id == passages[0].id]
    assert [item.revision for item in chain] == [1, 2]
    assert chain[1].supersedes == chain[0].id


def test_argument_map_uses_structural_evidence_not_invented_summary(tmp_path: Path) -> None:
    source = tmp_path / "paper.md"
    source.write_text(
        "# Paper\n\n## Introduction\n\nQuestion.\n\n## Methods\n\nMethod.\n\n## Results\n\nEvidence.\n\n## Limitations\n\nBoundary.\n",
        encoding="utf-8",
    )
    project = tmp_path / "project"
    init_project(project, "Paper", "en", "zh-CN")
    import_paper(project, source)
    segment_paper(project)
    points = build_argument_map(project)
    assert {item.category for item in points} == {"question", "method", "evidence", "boundary"}
    assert all(item.evidence_passage_ids for item in points)
    assert "Question." not in (project / "output" / "argument-map.md").read_text(encoding="utf-8")


def test_argument_map_prefers_numbered_method_result_and_discussion_sections(tmp_path: Path) -> None:
    source = tmp_path / "paper.md"
    source.write_text(
        "# Paper\n\n## Abstract\n\nAbstract.\n\n## 1 Introduction\n\nQuestion.\n\n## 4.1 Baseline model\n\nMethod.\n\n## 5.1 Results\n\nEvidence.\n\n## 7 Discussion\n\nBoundary.\n",
        encoding="utf-8",
    )
    project = tmp_path / "project"
    init_project(project, "Paper", "en", "zh-CN")
    import_paper(project, source)
    passages, _ = segment_paper(project)
    points = {item.category: item for item in build_argument_map(project)}
    by_id = {item.id: item for item in passages}
    assert by_id[points["question"].evidence_passage_ids[0]].section_title == "1 Introduction"
    assert by_id[points["method"].evidence_passage_ids[0]].section_title == "4.1 Baseline model"
    assert by_id[points["evidence"].evidence_passage_ids[0]].section_title == "5.1 Results"
    assert by_id[points["boundary"].evidence_passage_ids[0]].section_title == "7 Discussion"


def test_approved_glossary_and_entities_enter_context_with_source_evidence(tmp_path: Path) -> None:
    source = tmp_path / "paper.md"
    source.write_text("# Paper\n\n## Methods\n\nThe OECD used a panel.\n", encoding="utf-8")
    project = tmp_path / "project"
    init_project(project, "Paper", "en", "zh-CN")
    import_paper(project, source)
    passages, units = segment_paper(project)
    glossary = tmp_path / "glossary.jsonl"
    glossary.write_text(
        '{"term":"OECD","preferred_translation":"经济合作与发展组织","evidence_passage_ids":["'
        + passages[0].id + '"],"confidence":0.98,"status":"approved","note":"Official name."}\n',
        encoding="utf-8",
    )
    entities = tmp_path / "entities.jsonl"
    entities.write_text(
        '{"name":"OECD","kind":"organization","evidence_passage_ids":["'
        + passages[0].id + '"],"confidence":0.98,"status":"approved"}\n',
        encoding="utf-8",
    )
    assert import_glossary(project, glossary) == 1
    assert import_entities(project, entities) == 1
    context = build_context(project, units[0])
    assert context.glossary[0].preferred_translation == "经济合作与发展组织"
    assert context.entities[0].name == "OECD"
    with pytest.raises(ValueError, match="already exists"):
        import_glossary(project, glossary)
