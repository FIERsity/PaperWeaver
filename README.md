# PaperWeaver

PaperWeaver is an early-stage, file-first workspace for reading, translating, and publishing academic papers for Chinese readers.

It treats a paper as more than a sequence of paragraphs. The project will preserve its argument structure, figures, tables, citations, terminology, and reading path, then produce both a faithful translation and an intelligible article guide. The intended output is a readable Chinese paper edition—not a page-for-page imitation of an English journal PDF.

## Status

Version 0.2 imports Markdown, TXT, and JATS XML papers; retains JATS XML alongside normalized review Markdown; inventories figures, tables, displayed equations, citations, and references; and writes a reviewable reading guide as JSON and Markdown. Translation, DOCX/PDF import, figure binaries, terminology research, bilingual exports, and PDF/DOCX typesetting are planned next.

## Quick start

Python 3.11 or newer is required.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'

paperweaver init my-paper --title "A study"
paperweaver import my-paper examples/sample-paper.md  # or a JATS .xml article
paperweaver guide my-paper
```

This creates:

```text
my-paper/
├── paper.json
├── source/article.md
└── output/
    ├── reading-guide.json
    └── reading-guide.md
```

## What a guide contains

- Paper title and source metadata.
- Abstract (when marked by an `## Abstract` heading).
- Section map and word counts.
- A structure inventory for JATS figures, tables, equations, citations, and references.
- Reading questions that distinguish the research question, method, evidence, limits, and contribution.

The guide is intentionally an orientation aid, not an invented summary or a substitute for reading the source.

## Roadmap

1. Markdown foundation and reviewable reading guide.
2. DOCX and PDF-aware import with figure/table/citation inventory.
3. Terminology evidence and context-aware Chinese translation.
4. Translation critique, bilingual comparison, and selective revision.
5. Chinese academic PDF/DOCX/EPUB rendering with reflowed figures and tables.
6. Agent-generated article guides, method explainers, and source-grounded discussion questions.

## Relationship to ContextWeaver

PaperWeaver is a separate project from [ContextWeaver](https://github.com/FIERsity/ContextWeaver). Each has its own Git history, issues, releases, CI, dependencies, and project directory. PaperWeaver may later reuse ideas such as append-only revision history, source evidence, and model-independent adapters, but it does not vendor or duplicate ContextWeaver code. A shared implementation, if justified, must be extracted into a separately versioned and licensed package rather than added as a Git submodule or copied between repositories.

## Contributing

See [AGENTS.md](AGENTS.md) and [docs/architecture.md](docs/architecture.md). This project is pre-alpha; small, tested changes are preferred.
