# Shared helpers for test/<app>/run-tests.sh (sourced, not executed).
# Modes of test/<app>/run-tests.sh:
#   (none) | all   tests, then the GUI goldens
#   tests          test stages only           (CI job "test:<app>")
#   goldens        what the screenshots need + goldens (CI job "goldens:<app>", just <app>-gui-goldens)
SKIPPED=skipped
pipeline_mode() { MODE="${1:-all}"; case "$MODE" in all|tests|goldens) ;; *) echo "usage: $(basename "$0") [all|tests|goldens]" >&2; exit 2;; esac; }
runs_tests()   { [ "$MODE" != goldens ]; }
runs_goldens() { [ "$MODE" != tests ]; }
# JUnit report when PYTEST_JUNIT_DIR is set (CI)
pytest_report_args() { [ -n "${PYTEST_JUNIT_DIR:-}" ] && { mkdir -p "$PYTEST_JUNIT_DIR"; echo "--junitxml=$PYTEST_JUNIT_DIR/$1.xml"; }; }
# goldens dir, emptied first: a screen no longer captured shows up as a *deleted* golden
goldens_dir() { local d="$REPO/test/screenshot/$1"; mkdir -p "$d"; rm -f "$d"/*.png; echo "$d"; }
summary_line() { local l="$1" rc="$2" d="${3:-}"; if [ "$rc" = "$SKIPPED" ]; then printf ' %-12s: -\n' "$l"; elif [ "$rc" -eq 0 ]; then printf ' %-12s: OK%s\n' "$l" "${d:+ ($d)}"; else printf ' %-12s: FAILED (%s)\n' "$l" "$rc"; fi; }
all_ok() { local rc; for rc in "$@"; do [ "$rc" = "$SKIPPED" ] || [ "$rc" -eq 0 ] || return 1; done; }
