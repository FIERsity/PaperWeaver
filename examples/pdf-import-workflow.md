# PDF import workflow

Install the optional deterministic PDF stack:

```bash
python -m pip install -e '.[dev,pdf]'
```

Import into a fresh PaperWeaver project:

```bash
paperweaver init paper-workspace --title "Paper title"
paperweaver import paper-workspace paper.pdf
paperweaver pdf-status paper-workspace
paperweaver pdf-validate paper-workspace
```

A `complete` PDF can continue through the existing pipeline:

```bash
paperweaver segment paper-workspace
```

An `incomplete` PDF still contains useful review artifacts:

```text
paper-workspace/source/
├── original.pdf
├── article.md
├── article-map.jsonl
├── assets/
└── pdf/
    ├── manifest.json
    ├── qa.json
    ├── qa.md
    └── runs/<run_id>/
```

Review `source/pdf/qa.md` and the explicit image/table/equation placeholders in `article.md`. Boxed tables whose grid closes and whose cell characters are accounted render as pipe tables; their text cells and captions become independent Passages, while numeric cells remain literal and are restored through the render tree. Caption-bounded figure clusters render as `![Fig. N](assets/…)`, and simple text-layer equations render as verified display math with their original number. Unboxed/span tables, captionless clusters, complex equations and ambiguous layouts remain honest crops. `qa.json` lists `ocr_candidates`, but no OCR engine runs automatically. Do not edit `article.md` or the base block ledger to bypass the gate. The current release does not yet accept Agent repair drafts; unresolved content remains blocked until a later structural-recovery phase.

## Open-paper regression corpus

Developers can reproduce real-paper diagnostics without committing publisher files:

```bash
python scripts/pdf_corpus.py fetch
python scripts/pdf_corpus.py verify
python scripts/pdf_corpus.py run --tag tables --jobs 2
```

The manifest at `tests/corpus/pdf-jats-manifest.json` pins every source by size and SHA-256 and
records its primary landing page and reuse license. `fetch` downloads only missing files and
refuses changed bytes. `verify` is fully offline. `run` creates a fresh directory below
`tmp/corpus-runs/` and writes both `report.json` and `report.md`, while keeping each paper's
normal PaperWeaver project and QA artifacts available for review.

To run one cached real paper through pytest explicitly:

```bash
PAPERWEAVER_RUN_CORPUS=1 PAPERWEAVER_CORPUS_ID=joss-04061 \
  pytest tests/test_pdf_corpus.py -m corpus
```

Normal `pytest` skips this test and never accesses the network.
