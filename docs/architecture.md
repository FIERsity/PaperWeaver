# Architecture

PaperWeaver has three deliberately separate layers:

1. **Source retention** copies the imported paper and records its SHA-256 digest.
2. **Paper structure** identifies title, abstract, sections, and source line ranges without interpreting claims.
3. **Reader artifacts** derive source-grounded guides and later translations, reviews, and rendered editions.

The canonical state is transparent JSON and Markdown under a project directory. The source is authoritative. Guides are derived artifacts and must identify uncertainty rather than invent research conclusions.

The Markdown parser is intentionally conservative. It recognizes ATX headings and an `Abstract` section. The JATS importer retains the original XML, writes normalized Markdown for inspection, and produces a transparent inventory of figures, tables, displayed equations, bibliographic citations, references, and import limits. It preserves caption text as explicit markers but does not fetch figure binaries or claim mathematical layout fidelity. Future DOCX/PDF importers must satisfy the same structure-before-semantics rule.

## Repository boundary

PaperWeaver and ContextWeaver are sibling repositories, not a monorepo and not mutually nested repositories. A local parent directory may contain both, but each working tree has exactly one Git history and one primary remote. They communicate only through documented file contracts or future versioned packages. Git submodules and copied implementation are intentionally out of scope: they would couple releases before a shared abstraction has demonstrated a stable need.
