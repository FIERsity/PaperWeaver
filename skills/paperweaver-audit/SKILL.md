---
name: paperweaver-audit
description: Burn down unresolved PDF regions (borderless tables, complex equations) in a PaperWeaver project by proposing evidence-bound repairs through the audit door. Use when `paperweaver audit-status` reports open work orders and the user wants them repaired and materialized into the edition.
---

# PaperWeaver audit skill

You are the audit layer of PaperWeaver: you look at evidence and propose repairs.
The CLI is the judge. You never write project files, ledgers, or views by hand —
every contribution enters through `paperweaver audit-import` and only merges after
deterministic re-validation. Proposals carry **geometry or LaTeX only, never content
text**: table cells are rebuilt by the engine from the PDF's own characters.

## Workflow

1. **Orient.** In the project root run:
   ```bash
   paperweaver audit-status <project>
   paperweaver audit-export <project>
   ```
   Read `output/audit-package.json`. It holds one work order per repairable block,
   each with `bbox`, `crop` (a PNG you can view), `caption`, and `glyphs` —
   `[payload, x0, y0, x1, y1]` arrays for every character in the region. The glyph
   list is ground truth; the crop image is for structure inference only.
2. **Propose per work order, one JSONL line each.**

   `table_grid` line:
   ```json
   {"work_order_id": "…", "type": "table_grid",
    "grid": {"x_bounds": [36.0, 306.0, 576.0], "y_bounds": [625.6, 662.2, 698.8]},
    "header_rows": 1}
   ```
   Grid recipe (validated against real corpus tables):
   - Sort glyph spans on each axis; take the largest inter-glyph gaps as cut
     windows until you have the column/row count you see in the crop.
   - Slide each cut inside its window to **maximize the distance to the nearest
     glyph center** on that axis; a cut must end up > 2pt away from every glyph
     center (the validator counts a character whose center sits within 2pt of a
     boundary as ambiguous). Slicing a tall glyph's *bbox* is fine — assignment
     is by center — so dense math with stacked fractions still bands: use fewer,
     taller row bands and let multi-line records collapse into one cell.
   - Bounds must be strictly increasing and inside `bbox` ± 2pt.
   - `header_rows` comes from the caption and the top band of the crop; it must be
     smaller than the row count.
   - Try the most likely split first; if `verify-draft` rejects with a coverage or
     "outside" reason, adjust cuts toward the failing gap **once**, then stop.

   `equation_latex` line:
   ```json
   {"work_order_id": "…", "type": "equation_latex", "latex": "V_{p}(,) ="}
   ```
   LaTeX rules:
   - The validator requires **every region glyph to be consumed** (counted as a
     multiset), delimiters balanced, and no `(cid:…)` strings. Write the glyphs
     from the work order first, then add structure (`_{}`, `^{}`, `\frac`) that the
     crop justifies. Never introduce a symbol that is not in the glyph list.
3. **Self-check before submitting.** Write the draft to a scratch `.jsonl` and run:
   ```bash
   paperweaver verify-draft <project> draft.jsonl
   ```
   It validates without writing anything and prints per-line verdicts with reject
   reasons. Fix your draft accordingly. **Bounded retry: after one failed
   verification round per work order, submit what you have and leave the rest.**
4. **Submit, apply, report.**
   ```bash
   paperweaver audit-import <project> draft.jsonl --model <your-model-id>
   paperweaver audit-apply <project>
   paperweaver audit-status <project>
   ```
   Report to the user: accepted / rejected counts with reasons, the new status,
   and the work orders still open. Unresolved leftovers are for human review, not
   for another retry loop.

## Hard rules

- Never invent content: every table cell comes from the PDF's characters; every
  LaTeX token must trace to a region glyph.
- Never edit `source/`, `state/`, or any `*.jsonl` ledger directly; the only write
  path is `audit-import` (and `audit-apply`, which is engine-owned).
- One bounded retry per work order, then move on. The gate prefers an honest
  unresolved crop over a forced grid.
- If `audit-apply` fails with `AUDIT_APPLY_*` or `PDF_REPAIR_*`, surface the error
  verbatim to the user instead of "fixing" state by hand.
