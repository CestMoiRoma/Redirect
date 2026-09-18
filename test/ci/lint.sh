#!/usr/bin/env bash
# lint.sh       check only (just lint, CI job "lint"): exit 1 on any finding, nothing modified
# lint.sh fix   apply the formatters (just fmt); linters are not run
set -uo pipefail
REPO="${REPO:-/repo}"; cd "$REPO"
case "${1:-check}" in
  check) CI_LINT_CHECK=1 treefmt --no-cache --fail-on-change ;;
  fix)   treefmt --no-cache --formatters ruff-format,prettier ;;
  *) echo "usage: lint.sh [check|fix]" >&2; exit 2 ;;
esac
