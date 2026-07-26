"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .core import build_reading_guide, import_paper, init_project


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
    return root


def run(arguments: list[str] | None = None) -> int:
    args = parser().parse_args(arguments)
    if args.command == "init":
        init_project(args.project, args.title, args.source_language, args.target_language)
    elif args.command == "import":
        import_paper(args.project, args.source)
    else:
        build_reading_guide(args.project)
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(run())
