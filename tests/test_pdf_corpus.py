import argparse
import copy
import hashlib
import io
import json
import os
from pathlib import Path

import pytest

from scripts.pdf_corpus import (
    CorpusError,
    build_report,
    cached_path,
    diagnostic_tokens,
    fetch_file,
    floor_gate,
    lcs_length,
    load_floors,
    load_manifest,
    partitioned_diagnostics,
    pin_floors,
    render_report_markdown,
    run_paper,
    select_papers,
    token_diagnostics,
    verify_cached_file,
)

MANIFEST = Path(__file__).parent / "corpus" / "pdf-jats-manifest.json"


def test_open_pdf_manifest_is_licensed_diverse_and_pinned() -> None:
    manifest = load_manifest(MANIFEST)
    papers = manifest["papers"]
    assert len(papers) >= 12
    assert sum("jats" in paper["files"] for paper in papers) >= 3
    assert any("old-double-column" in paper["layout_tags"] for paper in papers)
    assert any("scanned" in paper["layout_tags"] for paper in papers)
    assert any("wide-tables" in paper["layout_tags"] for paper in papers)
    assert all(paper["license"]["identifier"] for paper in papers)
    assert all(paper["files"]["pdf"]["page_count"] > 0 for paper in papers)


def test_manifest_rejects_unknown_fields_and_unknown_selection(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    broken = copy.deepcopy(manifest)
    broken["papers"][0]["unexpected"] = True
    path = tmp_path / "broken.json"
    path.write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(CorpusError, match="CORPUS_MANIFEST_INVALID"):
        load_manifest(path)
    with pytest.raises(CorpusError, match="CORPUS_SELECTION_INVALID"):
        select_papers(manifest, {"not-a-paper"})


def test_cache_verification_refuses_changed_source_bytes(tmp_path: Path) -> None:
    source = tmp_path / "paper.pdf"
    data = b"%PDF-1.7\nfixed source\n"
    source.write_bytes(data)
    spec = {
        "url": "https://example.test/paper.pdf",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "page_count": 1,
    }
    verify_cached_file(source, spec, "pdf")
    source.write_bytes(data + b"changed")
    with pytest.raises(CorpusError, match="CORPUS_SOURCE_SIZE_MISMATCH"):
        verify_cached_file(source, spec, "pdf")


def test_fetch_is_atomic_and_reuses_verified_cache(tmp_path: Path, monkeypatch) -> None:
    data = b"%PDF-1.7\nnetwork fixture\n"
    spec = {
        "url": "https://example.test/paper.pdf",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "page_count": 1,
    }

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    calls = 0

    def open_fixture(*args, **kwargs):
        nonlocal calls
        calls += 1
        return Response(data)

    monkeypatch.setattr("scripts.pdf_corpus.urllib.request.urlopen", open_fixture)
    path = tmp_path / "cache" / "paper.pdf"
    assert fetch_file(path, spec, "pdf") == "downloaded"
    assert fetch_file(path, spec, "pdf") == "cached"
    assert path.read_bytes() == data
    assert calls == 1
    assert not list(path.parent.glob(".download-*"))


def test_multilingual_token_alignment_is_an_explicit_gate() -> None:
    reference = diagnostic_tokens("中文 ﬁgure 12.5")
    candidate = diagnostic_tokens("中文 figure 12.5 extra")
    assert reference == ["中", "文", "figure", "12", ".", "5"]
    result = token_diagnostics(reference, candidate)
    assert result["recall"] == 1.0
    assert result["precision"] < 1.0
    assert result["order_ratio"] == 1.0
    assert result["gating"] is True


def test_lcs_alignment_measures_order_without_quadratic_matrix() -> None:
    assert lcs_length(["a", "b", "c", "d"], ["a", "c", "b", "d"]) == 3
    assert lcs_length(["中", "文", "a"], ["文", "中", "a"]) == 2


def test_partitioned_diagnostics_scores_streams_separately() -> None:
    reference = {
        "prose": diagnostic_tokens("alpha beta"),
        "table": diagnostic_tokens("x y"),
    }
    candidate = {
        "prose": diagnostic_tokens("alpha extra beta"),
        "table": diagnostic_tokens("x y"),
    }
    result = partitioned_diagnostics(reference, candidate)
    assert result["method"] == "unicode-token-partitioned-v2"
    assert result["recall"] == 1.0
    assert result["precision"] == 4 / 5
    assert result["order_ratio"] == 1.0
    assert result["partitions"]["table"]["recall"] == 1.0
    assert result["gating"] is True


def test_partitioned_diagnostics_never_borrows_across_streams() -> None:
    reference = {"prose": diagnostic_tokens("a b"), "table": diagnostic_tokens("c")}
    candidate = {"prose": diagnostic_tokens("a c b"), "table": diagnostic_tokens("")}
    result = partitioned_diagnostics(reference, candidate)
    assert result["recall"] == 2 / 3
    assert result["partitions"]["table"]["recall"] == 0.0


def test_load_floors_validates_structure(tmp_path: Path) -> None:
    missing = tmp_path / "floors.json"
    with pytest.raises(CorpusError, match="CORPUS_FLOORS_MISSING"):
        load_floors(missing)
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps({"schema_version": 1, "papers": {}}), encoding="utf-8")
    with pytest.raises(CorpusError, match="CORPUS_FLOORS_INVALID"):
        load_floors(broken)
    bad_values = tmp_path / "bad-values.json"
    bad_values.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_run_id": "20260827T000000Z-test",
                "pinned_at": "2026-08-27T00:00:00Z",
                "guard": 0.002,
                "papers": {"pone-0251194": {"recall": 1.5, "precision": 0.5, "order_ratio": 0.5}},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(CorpusError, match="CORPUS_FLOORS_INVALID"):
        load_floors(bad_values)


def test_floor_gate_bites_when_measurement_drops() -> None:
    floors = {"recall": 0.9, "precision": 0.85, "order_ratio": 0.9}
    assert floor_gate({"recall": 0.9, "precision": 0.85, "order_ratio": 0.9}, floors)["passed"]
    dropped = floor_gate({"recall": 0.899, "precision": 0.85, "order_ratio": 0.9}, floors)
    assert dropped["passed"] is False


def test_pin_floors_writes_guarded_values_from_run_report(tmp_path: Path, capsys) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    report = {
        "run_id": "20260827T000000Z-test",
        "results": [
            {
                "id": "pone-0251194",
                "error": None,
                "token_diagnostics": {"recall": 0.9, "precision": 0.85, "order_ratio": 0.9},
            },
            {"id": "joss-04061", "error": None, "token_diagnostics": None},
        ],
    }
    (run_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
    floors = tmp_path / "floors.json"
    arguments = argparse.Namespace(source_run=run_dir, floors=floors, guard=0.002)
    assert pin_floors(arguments) == 0
    payload = json.loads(floors.read_text(encoding="utf-8"))
    assert payload["source_run_id"] == "20260827T000000Z-test"
    assert payload["papers"]["pone-0251194"] == {
        "recall": 0.898,
        "precision": 0.848,
        "order_ratio": 0.898,
    }
    assert "pin pone-0251194" in capsys.readouterr().out


def test_pin_floors_refuses_runs_with_errors(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    report = {
        "run_id": "20260827T000000Z-test",
        "results": [{"id": "acl-tables-2024", "error": "ValueError: test", "token_diagnostics": None}],
    }
    (run_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
    arguments = argparse.Namespace(source_run=run_dir, floors=tmp_path / "floors.json", guard=0.002)
    with pytest.raises(CorpusError, match="CORPUS_PIN_REFUSED"):
        pin_floors(arguments)


def test_report_is_stably_ordered_and_surfaces_errors(monkeypatch) -> None:
    manifest = load_manifest(MANIFEST)
    papers = select_papers(manifest, {"joss-04061", "acl-tables-2024"})
    monkeypatch.setattr(
        "scripts.pdf_corpus.environment_record",
        lambda: {"python": "test", "platform": "test", "packages": {}, "pdf_policy_sha256": "0" * 64},
    )
    results = [
        {
            "id": "joss-04061",
            "status": "incomplete",
            "duration_seconds": 1.25,
            "error": None,
            "semantic_gate": None,
            "status_gate": None,
            "metrics": {"pages": 5, "verified_figures": 1, "verified_tables": 0, "verified_equations": 0, "unresolved_blocks": 2},
        },
        {
            "id": "acl-tables-2024",
            "status": "fatal",
            "duration_seconds": 0.5,
            "error": "ValueError: test",
            "semantic_gate": None,
            "status_gate": None,
            "metrics": {},
        },
    ]
    report = build_report(manifest, papers, results, "20260827T000000Z-test")
    assert [item["id"] for item in report["results"]] == ["acl-tables-2024", "joss-04061"]
    markdown = render_report_markdown(report)
    assert "| `joss-04061` | incomplete | 5 |" in markdown
    assert "Error for `acl-tables-2024`" in markdown


@pytest.mark.corpus
@pytest.mark.skipif(
    os.environ.get("PAPERWEAVER_RUN_CORPUS") != "1",
    reason="set PAPERWEAVER_RUN_CORPUS=1 to run a checksum-pinned cached paper",
)
def test_cached_real_paper_import(tmp_path: Path) -> None:
    pytest.importorskip("pdfplumber")
    pytest.importorskip("pypdfium2")
    manifest = load_manifest(MANIFEST)
    paper_id = os.environ.get("PAPERWEAVER_CORPUS_ID", "joss-04061")
    paper = select_papers(manifest, {paper_id})[0]
    cache = Path(os.environ.get("PAPERWEAVER_CORPUS_CACHE", "tmp/corpus-cache"))
    verify_cached_file(cached_path(cache, paper_id, "pdf"), paper["files"]["pdf"], "pdf")
    floors = load_floors().get(paper_id) if "jats" in paper["files"] else None
    result = run_paper(paper, cache, tmp_path / "run", floors)
    assert result["error"] is None
    assert result["idempotent"] is True
    assert result["status"] in {"complete", "complete_with_warnings", "incomplete", "unsupported"}
    assert result["metrics"]["source_object_accounting_ratio"] == 1.0
    if paper["expected"]["required_status"] == "complete":
        from paperweaver.publication import render_translation_pdf
        from paperweaver.translation import (
            MockTranslationAdapter,
            export_translated_markdown,
            segment_paper,
            translate_paper,
        )

        assert result["status"] in {"complete", "complete_with_warnings"}
        project = tmp_path / "run" / "projects" / paper_id
        passages, _ = segment_paper(project, unit_size=2)
        passage_ids = [item.id for item in passages]
        assert [item.id for item in segment_paper(project, unit_size=2)[0]] == passage_ids
        translate_paper(project, MockTranslationAdapter())
        markdown = export_translated_markdown(project)
        rendered = render_translation_pdf(markdown)
        assert rendered.read_bytes().startswith(b"%PDF")
        assert len(list((project / "output" / "assets").glob("*.png"))) == 4
