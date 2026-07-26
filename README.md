# PaperWeaver

PaperWeaver is an early-stage, file-first workspace for reading, translating, and publishing academic papers for Chinese readers.

It treats a paper as more than a sequence of paragraphs. The project will preserve its argument structure, figures, tables, citations, terminology, and reading path, then produce both a faithful translation and an intelligible article guide. The intended output is a readable Chinese paper edition—not a page-for-page imitation of an English journal PDF.

## Status

Version 0.3 imports Markdown, TXT, and JATS XML papers; retains JATS XML alongside normalized review Markdown; inventories figures, tables, displayed equations, citations, and references; creates stable paper Passages and TranslationUnits; preserves append-only translation revisions; and writes source-grounded reading artifacts. The included mock translator makes the complete workflow testable without an API key. Online adapters, DOCX/PDF import, figure binaries, terminology research, and publication typesetting are planned next.

## Quick start

Python 3.11 or newer is required.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'

paperweaver init my-paper --title "A study"
paperweaver import my-paper examples/sample-paper.md  # or a JATS .xml article
paperweaver segment my-paper --unit-size 2
paperweaver argument-map my-paper
paperweaver translate my-paper
paperweaver validate my-paper
paperweaver export my-paper
paperweaver guide my-paper
```

This creates:

```text
my-paper/
├── paper.json
├── source/article.md
├── state/
│   ├── passages.jsonl
│   ├── units.jsonl
│   └── translations.jsonl
└── output/
    ├── argument-map.md
    ├── bilingual.md
    ├── reading-guide.json
    └── reading-guide.md
```

## What a guide contains

- Paper title and source metadata.
- Abstract (when marked by an `## Abstract` heading).
- Section map and word counts.
- A structure inventory for JATS figures, tables, equations, citations, and references.
- Reading questions that distinguish the research question, method, evidence, limits, and contribution.

`argument-map` is a deliberately conservative article-understanding artifact. It maps Introduction/Methods/Results/Discussion-like sections to exact Passage IDs and tells the reader where to inspect research question, method/identification, evidence, and conclusion boundaries. It does not restate findings as an Agent-generated summary.

## Translation workflow

`segment` derives stable IDs from the imported source digest, section title, ordinal, and normalized text. A `TranslationUnit` stays inside one paper section and includes only immediate neighboring context plus approved glossary/entity evidence. `translate` is resumable; `translation-import` accepts strict Agent JSONL (`passage_id`, `translated_text`) and appends revisions rather than overwriting records. Use `--passage ID --reason terminology-fix` for an intentional selective retranslation. `export` writes bilingual Markdown only after complete Passage coverage validates.

`glossary-import` and `entity-import` accept separate strict JSONL rows. Every row must cite real `evidence_passage_ids`; duplicate terms/entities are rejected rather than overwritten. Only rows whose status is `approved` enter a `TranslationContext`.

The guide is intentionally an orientation aid, not an invented summary or a substitute for reading the source.

## Roadmap

1. Markdown foundation and reviewable reading guide.
2. DOCX and PDF-aware import with figure/table/citation inventory.
3. Terminology/entity extraction and sourced terminology adjudication.
4. Provider-neutral online adapters, translation critique, bilingual comparison, and selective revision.
5. Chinese academic PDF/DOCX/EPUB rendering with reflowed figures and tables.
6. Agent-generated source-grounded article explanations, method explainers, and discussion questions.

## Relationship to ContextWeaver

PaperWeaver is a separate project from [ContextWeaver](https://github.com/FIERsity/ContextWeaver). Each has its own Git history, issues, releases, CI, dependencies, and project directory. PaperWeaver may later reuse ideas such as append-only revision history, source evidence, and model-independent adapters, but it does not vendor or duplicate ContextWeaver code. A shared implementation, if justified, must be extracted into a separately versioned and licensed package rather than added as a Git submodule or copied between repositories.

## Contributing

See [AGENTS.md](AGENTS.md) and [docs/architecture.md](docs/architecture.md). This project is pre-alpha; small, tested changes are preferred.
