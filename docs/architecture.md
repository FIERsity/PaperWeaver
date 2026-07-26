# Architecture

PaperWeaver has three deliberately separate layers:

1. **Source retention** copies the imported paper and records its SHA-256 digest.
2. **Paper structure** identifies title, abstract, sections, and source line ranges without interpreting claims.
3. **Reader artifacts** derive source-grounded guides and later translations, reviews, and rendered editions.

The canonical state is transparent JSON and Markdown under a project directory. The source is authoritative. Guides are derived artifacts and must identify uncertainty rather than invent research conclusions.

The initial Markdown parser is intentionally conservative. It recognizes ATX headings and an `Abstract` section. Future JATS/DOCX/PDF importers must preserve structure before attempting semantic processing, especially figures, tables, equations, captions, citations, and footnotes.
