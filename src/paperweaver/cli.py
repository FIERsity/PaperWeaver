"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .core import build_reading_guide, import_paper, init_project
from .translation import (
    MockTranslationAdapter,
    export_bilingual_markdown,
    import_entities,
    import_glossary,
    import_translation_draft,
    segment_paper,
    translate_paper,
    validate_translations,
)
from .understanding import build_argument_map


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="paperweaver")
    commands = root.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create a paper workspace")
    init.add_argument("project", type=Path)
    init.add_argument("--title", required=True)
    init.add_argument("--source-language", default="en")
    init.add_argument("--target-language", default="zh-CN")
    imported = commands.add_parser("import", help="Import a Markdown, TXT, or JATS XML paper")
    imported.add_argument("project", type=Path)
    imported.add_argument("source", type=Path)
    guide = commands.add_parser("guide", help="Write source-grounded reading guide artifacts")
    guide.add_argument("project", type=Path)
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
    glossary = commands.add_parser("glossary-import", help="Append evidence-backed glossary rows from JSONL")
    glossary.add_argument("project", type=Path)
    glossary.add_argument("draft", type=Path)
    entities = commands.add_parser("entity-import", help="Append evidence-backed entity rows from JSONL")
    entities.add_argument("project", type=Path)
    entities.add_argument("draft", type=Path)
    validate = commands.add_parser("validate", help="Validate one-to-one paper Passage translations")
    validate.add_argument("project", type=Path)
    exported = commands.add_parser("export", help="Export bilingual Markdown after validation")
    exported.add_argument("project", type=Path)
    argument_map = commands.add_parser("argument-map", help="Write source-grounded research reading map")
    argument_map.add_argument("project", type=Path)
    return root


def run(arguments: list[str] | None = None) -> int:
    args = parser().parse_args(arguments)
    if args.command == "init":
        init_project(args.project, args.title, args.source_language, args.target_language)
    elif args.command == "import":
        import_paper(args.project, args.source)
    elif args.command == "guide":
        build_reading_guide(args.project)
    elif args.command == "segment":
        segment_paper(args.project, args.unit_size)
    elif args.command == "translate":
        translate_paper(
            args.project, MockTranslationAdapter(), passage_ids=set(args.passage) or None,
            reason=args.reason, max_units=args.max_units,
        )
    elif args.command == "translation-import":
        import_translation_draft(args.project, args.draft, args.adapter, args.model, args.reason)
    elif args.command == "glossary-import":
        import_glossary(args.project, args.draft)
    elif args.command == "entity-import":
        import_entities(args.project, args.draft)
    elif args.command == "validate":
        errors = validate_translations(args.project)
        if errors:
            for error in errors:
                print(error)
            return 1
    elif args.command == "export":
        export_bilingual_markdown(args.project)
    else:
        build_argument_map(args.project)
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(run())
