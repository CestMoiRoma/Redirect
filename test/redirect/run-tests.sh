#!/usr/bin/env bash
# redirect pipeline: pytest (unit, API, redirects, storage, structure), then the end-to-end scenario + GUI goldens.
#
# No front-end unit stage (no vitest): the front end is plain JavaScript served as-is, with no build step
# and no node toolchain in the app. Its checks (verbatim texts, visible elements, button behaviour, copy,
# dialogs, errors) run in a real browser against the real app, in the end-to-end scenario (screenshots.py).
set -uo pipefail
REPO="${REPO:-/repo}"
source "$REPO/test/ci/pipeline.sh"
pipeline_mode "${1:-}"
cd "$REPO/test/redirect"
# The app is imported from the checkout, never installed
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"
PYTEST_RC=$SKIPPED E2E_RC=$SKIPPED E2E_DETAIL=""

if runs_tests; then
  # Unquoted on purpose: expands to nothing, or to one argument without spaces
  python -m pytest -q --color=no -p no:cacheprovider $(pytest_report_args redirect)
  PYTEST_RC=$?
fi

if runs_goldens; then
  SHOTS="$(goldens_dir redirect)"
  python screenshots.py "$SHOTS"   # end-to-end scenario + captures
  E2E_RC=$?
  E2E_DETAIL="$(find "$SHOTS" -name '*.png' | wc -l | tr -d ' ') images"
fi

summary_line pytest "$PYTEST_RC"; summary_line e2e "$E2E_RC" "$E2E_DETAIL"
all_ok "$PYTEST_RC" "$E2E_RC"
