from pathlib import Path

import pytest

from paperweaver.cli import run
from paperweaver.core import build_reading_guide, import_paper, init_project, parse_sections


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
    assert any("method" in question.casefold() for question in guide.questions)
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
