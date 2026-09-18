#!/usr/bin/env bash
# treefmt formatter wrapper: applies a formatter, or only checks when CI_LINT_CHECK=1 (just lint).
set -euo pipefail
tool="$1"; shift
check="${CI_LINT_CHECK:-}"
case "$tool" in
  ruff-format) if [ -n "$check" ]; then exec ruff format --check --diff -- "$@"; else exec ruff format -- "$@"; fi ;;
  prettier)    if [ -n "$check" ]; then exec prettier --check -- "$@"; else exec prettier --write --log-level warn -- "$@"; fi ;;
  *) echo "format.sh: unknown formatter $tool" >&2; exit 2 ;;
esac
