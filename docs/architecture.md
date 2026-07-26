# Architecture

PaperWeaver has three deliberately separate layers:

1. **Source retention** copies the imported paper and records its SHA-256 digest.
2. **Paper structure** identifies title, abstract, sections, and source line ranges without interpreting claims.
3. **Translation state** derives stable Passages and section-bounded TranslationUnits, then stores append-only TranslationRecords.
4. **Reader artifacts** derive source-grounded guides, argument maps, and later rendered editions.

The canonical state is transparent JSONL and Markdown under a project directory. The source is authoritative. A Passage ID is a SHA-256-derived value over source digest, section title, source ordinal, and normalized text. Translation revisions link to their predecessor and never overwrite history. Guides are derived artifacts and must identify uncertainty rather than invent research conclusions.

The Markdown parser is intentionally conservative. It recognizes ATX headings and an `Abstract` section. Extracted TXT sources may contain common numbered paper headings such as `4 Data and methods`; these are normalized to reviewable Markdown headings, while title metadata in a `TITLE:` line becomes the document title. The JATS importer retains the original XML, writes normalized Markdown for inspection, and produces a transparent inventory of figures, tables, displayed equations, bibliographic citations, references, and import limits. It preserves caption text as explicit markers but does not fetch figure binaries or claim mathematical layout fidelity. Future DOCX/PDF importers must satisfy the same structure-before-semantics rule.

## Translation and evidence

Each TranslationUnit contains source Passages from one paper section, immediate adjacent text, and only approved terminology/entity evidence. Translation adapters must return one non-empty output per requested Passage. `translations.jsonl` is append-only; a selective retranslation appends a new record with `supersedes`, so a reviewer can reconstruct every decision. The mock adapter is for workflow validation only.

`glossary.jsonl` and `entities.jsonl` are evidence-bound collections. Imported rows must reference extant Passage IDs and cannot silently replace an existing term/entity name. Proposed rows remain outside translation context; an approved row is an explicit, inspectable decision. Future terminology extraction and authority research must append candidates or revisions rather than weaken this rule.

`argument-map` is not a semantic summary engine. It creates four structural reading tasks only when a matching paper section exists: research question, method/identification, evidence, and conclusion boundary. Every task names the exact supporting Passage IDs. A future Agent explanation layer may elaborate these tasks only by attaching its evidence Passage IDs and distinguishing source statements from its explanatory inference.

## Repository boundary

PaperWeaver and ContextWeaver are sibling repositories, not a monorepo and not mutually nested repositories. A local parent directory may contain both, but each working tree has exactly one Git history and one primary remote. They communicate only through documented file contracts or future versioned packages. Git submodules and copied implementation are intentionally out of scope: they would couple releases before a shared abstraction has demonstrated a stable need.
