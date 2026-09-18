#!/usr/bin/env bash
# treefmt linter: mypy --strict on the whole app package. The file list treefmt passes is ignored on
# purpose: a type error in one module can come from a change in another.
set -euo pipefail
exec mypy --strict --no-error-summary --python-version 3.12 -p redirect
