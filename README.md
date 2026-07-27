# PaperWeaver

PaperWeaver is a small, file-first workspace for turning an academic paper into two Chinese deliverables:

1. a complete translated Markdown document and a print-oriented A4 PDF;
2. a Chinese whole-paper summary covering the paper's content, methods, conclusions, and stated limitations.

It deliberately does not try to be a general reading-guide, knowledge-graph, or terminology-management application. Stable translation units, append-only revisions, and source evidence remain because they make the two deliverables reviewable and resumable.

## Status

Version 0.4 imports Markdown, TXT, and JATS XML, preserves the original source digest, derives stable paragraph Passages, accepts append-only Agent translations, validates complete coverage, and exports `translated.md` plus `pdf/translated.pdf`. For JATS sources, the edition also retains authors and affiliations, downloads source figures and tables into `output/assets/` at their original positions, and appends the original-language reference list. It also accepts an evidence-cited Chinese summary draft and exports `中文全文摘要.md`. On macOS, PDF output uses the system Songti font collection for Chinese and Times Roman for Latin runs; other platforms fall back to the bundled CJK PDF font.

PDF/DOCX source import, online model adapters, embedded figure binaries, automatic summary generation, and journal-faithful layout are intentionally out of scope for now.

## Quick start

Python 3.11 or newer is required.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'

paperweaver init my-paper --title "论文中文题名"
paperweaver import my-paper paper.xml
paperweaver segment my-paper --unit-size 2

# Append a strict Agent-produced JSONL translation draft, then validate and publish.
paperweaver translation-import my-paper translations.jsonl --model my-model
paperweaver validate my-paper
paperweaver export-translation my-paper

# Import and publish the Chinese whole-paper summary.
paperweaver summary-import my-paper summary.json --model my-model
paperweaver export-summary my-paper
```

`translations.jsonl` must contain one JSON object per source Passage:

```json
{"passage_id":"psg_example","translated_text":"对应的中文译文。"}
```

`summary.json` must be one JSON object with exactly these fields:

```json
{
  "overview": "全文中文概述。",
  "methods": "数据、设计与方法。",
  "conclusions": "论文报告的结论。",
  "limitations": "作者陈述的局限与适用边界。",
  "evidence_passage_ids": ["psg_example"]
}
```

The output directory contains only the two user-facing deliverables:

```text
output/
├── translated.md
├── pdf/translated.pdf
└── 中文全文摘要.md
```

`export-translation` refuses incomplete translation coverage. `summary-import` requires every summary to cite imported Passage IDs. Both choices favor an explicit failure over a plausible-looking but incomplete publication.

## JATS edition contract

For a JATS paper whose figures and tables use PLOS DOI graphic resources, `export-translation` always includes, in source order: Chinese main text and headings; authors and affiliations; original figures and tables with Chinese captions; display equations on their own centered lines; and the original-language reference list. The layout layer, rather than an Agent's formatting habits, detects these source structures. A missing or invalid graphic resource stops the export with an error.

## Development

See [AGENTS.md](AGENTS.md) and [docs/architecture.md](docs/architecture.md). This project is pre-alpha; keep changes small, tested, and compatible with existing translation records.
