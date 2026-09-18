#!/usr/bin/env bash
# Fails when test/screenshot (or test/screenshot/APP) differs from HEAD: modified, deleted or new files.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
target="test/screenshot${1:+/$1}"
changes="$(git status --porcelain --untracked-files=all -- "$target")"
if [ -n "$changes" ]; then
  echo "✗ GUI goldens differ from the commit ($target):"; echo "$changes"
  echo "Intended UI change: just ${1:-all}-gui-goldens, review the images, commit them. Otherwise: regression."
  exit 1
fi
echo "✓ GUI goldens identical to the commit ($target)"
