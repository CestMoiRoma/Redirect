#!/usr/bin/env bash
# Regenerates an app's goldens RUNS times and compares the SHA-256 of every PNG with run 1. Exit 1 on any difference.
set -uo pipefail
APP="${1:?usage: goldens-stability.sh APP [RUNS]}"; RUNS="${2:-3}"
REPO="${REPO:-/repo}"; DIR="$REPO/test/screenshot/$APP"; WORK="$(mktemp -d)"
for run in $(seq 1 "$RUNS"); do
  "$REPO/test/$APP/run-tests.sh" goldens > "$WORK/run$run.log" 2>&1 || { tail -40 "$WORK/run$run.log"; exit 1; }
  (cd "$DIR" && sha256sum -- *.png) > "$WORK/run$run.sha256"
  echo "run $run: $(wc -l < "$WORK/run$run.sha256" | tr -d ' ') images"
done
status=0
for run in $(seq 2 "$RUNS"); do
  diff "$WORK/run1.sha256" "$WORK/run$run.sha256" && echo "run $run: identical" || { echo "run $run: DIFFERENT"; status=1; }
done
exit $status
