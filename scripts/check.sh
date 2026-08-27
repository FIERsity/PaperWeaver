#!/usr/bin/env bash
# The single pre-commit gate: lint, tests, and the checksum-pinned corpus slice.
# Red means do not commit. Set CHECK_SKIP_CORPUS=1 to skip the corpus leg.
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON=${PYTHON:-.venv/bin/python}
if [ ! -x "$PYTHON" ]; then
  PYTHON=python
fi
RUFF=.venv/bin/ruff
if [ ! -x "$RUFF" ]; then
  RUFF=ruff
fi

"$RUFF" check .
"$PYTHON" -m pytest -q

if [ "${CHECK_SKIP_CORPUS:-0}" = "1" ]; then
  echo "check: corpus slice skipped (CHECK_SKIP_CORPUS=1)"
  exit 0
fi

SLICE=(
  --id pone-0251194
  --id pcbi-1005331
  --id pcbi-1002377
  --id pcbi-1012374
  --id acl-tables-2024
  --id joss-04061
  --id layoutparser-2103-15348
)
"$PYTHON" scripts/pdf_corpus.py fetch "${SLICE[@]}"
"$PYTHON" scripts/pdf_corpus.py run --jobs 4 "${SLICE[@]}"
