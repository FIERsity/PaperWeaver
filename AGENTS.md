# Agent instructions

Read `README.md`, `docs/strategy.md`, `docs/architecture.md`, `pyproject.toml`, and the relevant tests before modifying code. `docs/strategy.md` is authoritative when documents disagree.

- Preserve imported source text and its digest; never silently replace a source.
- Keep paper structure, translation state, and guide claims separately reviewable.
- Do not invent paper findings, methods, limitations, or references in a reading guide.
- Treat translations and summaries as revisions, not mutable source truth.
- Keep model/provider adapters outside domain and rendering logic.
- Preserve figures, tables, equations, citations, and their source locators when adding importers.
- Add tests for every behavior change and update examples/documentation.
- Prefer small, verifiable features; run `scripts/check.sh` before submitting. Red means do not commit.
- Every model contribution (translation draft or repair proposal) enters only through the strict JSONL proposal protocol and merges only after CLI-side validation. Never hand-edit generated artifacts, ledgers, or floors.
