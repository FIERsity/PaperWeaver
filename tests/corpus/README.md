# Open PDF regression corpus

`pdf-jats-manifest.json` is the versioned source of truth for PaperWeaver's real-paper
regression corpus. It records only open-paper metadata, provenance, licenses, layout test
intent, exact byte sizes, page counts, and SHA-256 digests. Paper and JATS bytes are not
committed and are not covered by PaperWeaver's MIT license.

Bootstrap or verify the ignored local cache explicitly:

```bash
python scripts/pdf_corpus.py fetch
python scripts/pdf_corpus.py verify
```

Run every cached paper or select a smaller diagnostic slice:

```bash
python scripts/pdf_corpus.py run --jobs 2
python scripts/pdf_corpus.py run --tag tables --jobs 2
python scripts/pdf_corpus.py run --id pone-0251194
```

Downloads are written to `tmp/corpus-cache/`; import workspaces and aggregate JSON/Markdown
reports are written to a new timestamped directory under `tmp/corpus-runs/`. Existing files
with different bytes are rejected. The scripts never refresh checksums or overwrite a prior
run.

The PLOS JATS files are immutable comparison oracles. Their element counts are checked before
each run, but their text never fills gaps in PDF extraction. The runner compares JATS body tokens
with PDF body blocks using an exact bit-parallel LCS/shortest-edit alignment. Reviewed per-paper
minimum recall, precision, and order ratios are regression gates: a drop makes the run and the
scheduled corpus workflow fail. These initial minima preserve the measured baseline; the design
target remains at least 99.5% recall/precision and 99% order as layout recovery improves.
The manifest can also require a completion status. `joss-04061` currently must remain complete;
its opt-in pytest path additionally segments, mock-translates, copies all four figures, and renders
the translated A4 PDF.

Default `pytest` remains offline and uses synthetic fixtures. To exercise a locally cached real
paper explicitly, set `PAPERWEAVER_RUN_CORPUS=1` and optionally
`PAPERWEAVER_CORPUS_ID=<paper-id>` before running `pytest tests/test_pdf_corpus.py`.

When adding or updating a paper:

1. Use a primary repository and record an explicit reuse license or public-domain statement.
2. Download manually into the ignored cache, inspect the paper, and record the exact bytes.
3. Update URL, byte size, page count, SHA-256, layout intent, and any JATS element counts in one
   reviewed change.
4. Never change a checksum merely because a source URL started returning different bytes;
   investigate and review the source revision first.
5. Record approved PDF/JATS edition differences before relaxing any regression expectation.
