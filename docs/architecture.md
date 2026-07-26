# Architecture

PaperWeaver has one narrow purpose: produce a complete Chinese translation edition and a source-grounded Chinese whole-paper summary.

The source is copied into `source/` and identified by SHA-256. `segment` turns normalized Markdown into stable paragraph Passages and section-bounded TranslationUnits. A Passage ID includes the source digest, section, ordinal, and normalized text; an ID change is therefore a migration concern, not a cosmetic refactor.

Translations are append-only `TranslationRecord`s in `state/translations.jsonl`. A revised translation links to the record it supersedes. Export reads the latest record for every non-structural Passage and refuses to write an edition if any are missing. The edition renderer writes `output/translated.md`, then a reflowed A4 PDF at `output/pdf/translated.pdf`. Chinese text uses Songti and Latin runs use Times Roman; it is a readable edition rather than a page-for-page recreation of a journal PDF.

Chinese whole-paper summaries are separate append-only `ChineseSummaryRecord`s in `state/chinese-summaries.jsonl`. Every record supplies four required parts: overview, methods, conclusions, and limitations. It also cites imported Passage IDs. `export-summary` renders only the newest revision to `output/中文全文摘要.md`; the source and prior summary records remain intact for review.

The CLI exposes only the workflow required to create those deliverables: project initialization/import, segmentation, translation intake and validation, translation export, summary intake, and summary export. Guides, argument maps, glossary/entity interfaces, and bilingual output are intentionally not part of the current product surface.
