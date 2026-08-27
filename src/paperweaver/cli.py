"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from .core import import_paper, init_project
from .pdf_contracts import PdfUnsupportedError, pdf_status
from .publication import render_translation_pdf
from .summary import export_chinese_summary, import_chinese_summary
from .translation import (
    MockTranslationAdapter,
    export_translated_markdown,
    import_translation_draft,
    segment_paper,
    translate_paper,
    validate_translations,
)

logger = logging.getLogger(__name__)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="paperweaver")
    commands = root.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create a paper workspace")
    init.add_argument("project", type=Path)
    init.add_argument("--title", required=True)
    init.add_argument("--source-language", default="en")
    init.add_argument("--target-language", default="zh-CN")
    imported = commands.add_parser("import", help="Import a Markdown, TXT, JATS XML, or PDF paper")
    imported.add_argument("project", type=Path)
    imported.add_argument("source", type=Path)
    imported.add_argument("--pdf-policy", type=Path)
    segment = commands.add_parser("segment", help="Create stable paper Passages and TranslationUnits")
    segment.add_argument("project", type=Path)
    segment.add_argument("--unit-size", type=int, default=2)
    translate = commands.add_parser("translate", help="Translate pending paper units with the offline mock")
    translate.add_argument("project", type=Path)
    translate.add_argument("--passage", action="append", default=[])
    translate.add_argument("--reason", default="initial")
    translate.add_argument("--max-units", type=int)
    draft = commands.add_parser("translation-import", help="Append Agent-produced paper translations")
    draft.add_argument("project", type=Path)
    draft.add_argument("draft", type=Path)
    draft.add_argument("--adapter", default="paper-agent")
    draft.add_argument("--model", required=True)
    draft.add_argument("--reason", default="agent-import")
    validate = commands.add_parser("validate", help="Validate one-to-one paper Passage translations")
    validate.add_argument("project", type=Path)
    exported = commands.add_parser("export-translation", help="Export complete translated Markdown and A4 PDF")
    exported.add_argument("project", type=Path)
    summary = commands.add_parser("summary-import", help="Append a sourced Chinese whole-paper summary JSON")
    summary.add_argument("project", type=Path)
    summary.add_argument("draft", type=Path)
    summary.add_argument("--adapter", default="paper-agent")
    summary.add_argument("--model", required=True)
    summary_export = commands.add_parser("export-summary", help="Export the latest Chinese whole-paper summary")
    summary_export.add_argument("project", type=Path)
    status = commands.add_parser("pdf-status", help="Show PDF import QA status")
    status.add_argument("project", type=Path)
    status.add_argument("--json", action="store_true")
    pdf_validate = commands.add_parser("pdf-validate", help="Apply the PDF import completion gate")
    pdf_validate.add_argument("project", type=Path)
    return root


def run(arguments: list[str] | None = None) -> int:
    args = parser().parse_args(arguments)
    if args.command == "init":
        init_project(args.project, args.title, args.source_language, args.target_language)
    elif args.command == "import":
        imported = import_paper(args.project, args.source, pdf_policy=args.pdf_policy)
        if imported.format == "pdf":
            return _pdf_exit_code(pdf_status(args.project))
    elif args.command == "segment":
        segment_paper(args.project, args.unit_size)
    elif args.command == "translate":
        translate_paper(
            args.project, MockTranslationAdapter(), passage_ids=set(args.passage) or None,
            reason=args.reason, max_units=args.max_units,
        )
    elif args.command == "translation-import":
        import_translation_draft(args.project, args.draft, args.adapter, args.model, args.reason)
    elif args.command == "validate":
        errors = validate_translations(args.project)
        if errors:
            for error in errors:
                print(error)
            return 1
    elif args.command == "export-translation":
        render_translation_pdf(export_translated_markdown(args.project))
    elif args.command == "summary-import":
        import_chinese_summary(args.project, args.draft, args.adapter, args.model)
    elif args.command == "export-summary":
        export_chinese_summary(args.project)
    elif args.command == "pdf-status":
        pdf_status(args.project)
        manifest = json.loads(
            (args.project / "source" / "pdf" / "manifest.json").read_text(encoding="utf-8")
        )
        print(json.dumps(manifest, ensure_ascii=False, indent=2) if args.json else manifest["status"])
    else:
        status = pdf_status(args.project)
        qa = json.loads(
            (args.project / "source" / "pdf" / "qa.json").read_text(encoding="utf-8")
        )
        for issue in qa["issues"]:
            print(f"{issue['severity']} {issue['code']}: {issue['message']}")
        return _pdf_exit_code(status)
    return 0


def _pdf_exit_code(status: str) -> int:
    return {"complete": 0, "complete_with_warnings": 0, "incomplete": 2,
            "unsupported": 3, "fatal": 1}[status]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        code = run()
    except PdfUnsupportedError as error:
        logger.error("%s", error)
        code = 3
    except (FileExistsError, FileNotFoundError, RuntimeError, ValueError) as error:
        logger.error("%s", error)
        code = 1
    raise SystemExit(code)
