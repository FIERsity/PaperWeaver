# PaperWeaver

PaperWeaver is a small, file-first workspace for turning an academic paper into two Chinese deliverables:

1. a complete translated Markdown document and a print-oriented A4 PDF;
2. a Chinese whole-paper summary covering the paper's content, methods, conclusions, and stated limitations.

It deliberately does not try to be a general reading-guide, knowledge-graph, or terminology-management application. Stable translation units, append-only revisions, and source evidence remain because they make the two deliverables reviewable and resumable.

## Status

The product form, one-door proposal protocol, and quality gates are pinned in [the global strategy](docs/strategy.md).
Version 0.5 adds the first deterministic PDF import slice. With the optional `[pdf]` dependencies, born-digital journal PDFs are copied unchanged to `source/original.pdf`, identified by SHA-256, converted into page/bbox-located blocks, and rendered as an anchored `source/article.md` with machine-readable and human-readable QA reports. Repeated text or visual headers and page numbers are retained in the evidence ledger but excluded from Markdown; full-width blocks split local one/two-column reading bands deterministically.

The PDF gate is intentionally conservative. Any unresolved glyph, page/vector region, table structure, equation, or ambiguous layout makes the import `incomplete` and prevents `segment`. The implemented P2/P3 core resolves boxed tables (verified when the rule grid closes and every cell character is accounted) into pipe tables, projects translatable cells into Passage slots, and reassembles their translations without exposing Markdown syntax to the model. Caption-bounded image/vector clusters become `figure` blocks with content-addressed crops and separate translatable captions. Simple selectable display equations are promoted only when every baseline/script/number glyph is consumed by a restricted parser; other equations stay unresolved crops. Unboxed/span tables and ambiguous or captionless figures remain honest crops.

PDF translation export now walks the same render tree as source materialization: figures, translated captions, literal formulas, table cells, references, and assets retain their structure, and pipe tables render as real tables in the A4 PDF. QA also emits page/block `ocr_candidates`; no OCR engine runs automatically, and rotation alone never triggers OCR. Agent repair drafts, scanned-page OCR, and complex formula/table recovery remain later phases described in [the PDF import design](docs/pdf-import-design.md).

Markdown, TXT, and JATS XML retain the v0.4 workflow: stable paragraph Passages, append-only Agent translations, complete coverage validation, `translated.md` plus `pdf/translated.pdf`, and an evidence-cited Chinese summary. JATS editions also retain authors, affiliations, source visuals, and original-language references.

Scanned PDF OCR, complex/raster formula and unboxed/span table recovery, DOCX source import, online model adapters, automatic summary generation, and journal-faithful layout are intentionally out of scope for now.

## Quick start

Python 3.11 or newer is required.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,pdf]'

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

For a PDF source, inspect the deterministic gate before segmentation:

```bash
paperweaver import my-paper paper.pdf
paperweaver pdf-status my-paper
paperweaver pdf-validate my-paper
paperweaver segment my-paper  # accepted only when PDF status is complete
```

Unresolved tables and equations can be handed to a model for evidence-bound repair:

```bash
paperweaver audit-export my-paper                        # output/audit-package.json
paperweaver verify-draft my-paper draft.jsonl            # self-check, writes nothing
paperweaver audit-import my-paper draft.jsonl --model my-model
paperweaver audit-apply my-paper                         # materialize accepted repairs
paperweaver audit-status my-paper                        # burn-down and acceptance rate
```

`audit-apply` rebuilds `article.md`, the article map, and the render tree from the immutable base run plus the accepted proposals; when the applied view has no unresolved block left, the import rises to `complete_with_repair`, which `segment` accepts like other complete statuses. The base ledger is never modified, and every applied proposal stays recorded in `transformations` and in the manifest `repairs` section.

Proposals never carry content text: a table proposal contributes only grid geometry whose cells the engine rebuilds from the PDF's own characters, and an equation proposal must have every region glyph consumed by balanced, cid-free LaTeX. Every submission is appended to the state ledger with its validation verdict.

`import` returns 0 for complete, 2 for incomplete, and 3 for a valid PDF outside the active born-digital policy. A fatal parse/backend error returns 1 and does not commit a PDF source.

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

See [AGENTS.md](AGENTS.md), [docs/architecture.md](docs/architecture.md), and [examples/pdf-import-workflow.md](examples/pdf-import-workflow.md). This project is pre-alpha; keep changes small, tested, and compatible with existing translation records.

Real-paper PDF regression uses a checksum-pinned open corpus in
[`tests/corpus/pdf-jats-manifest.json`](tests/corpus/pdf-jats-manifest.json). The committed
manifest currently covers PLOS PDF/JATS pairs, modern and older one/two-column papers,
table/equation/figure-heavy layouts, and public-domain scans. Paper bytes stay in ignored
`tmp/corpus-cache/`; bootstrap and batch runs are always explicit:

```bash
python scripts/pdf_corpus.py fetch
python scripts/pdf_corpus.py verify
python scripts/pdf_corpus.py run --jobs 2
```

Default `pytest` never downloads papers. See [`tests/corpus/README.md`](tests/corpus/README.md)
for license, checksum, selection, and opt-in integration-test rules.
The corpus runner enforces reviewed JATS token/order minima, and `joss-04061` is the first
checksum-pinned real PDF required to remain `complete` and pass the full structural export path.
