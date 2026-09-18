#!/usr/bin/env bash
# App image test (just image-test, CI job "image:app"): build the image of the root Dockerfile, run it the way
# a user would, and check what the image adds on top of the code: packaging, user, volume, health check,
# served files, proxy settings, persistence, clean stop. Uses the host's Docker, never the CI image.
#
#   image-test.sh [PLATFORM [IMAGE]]
#     PLATFORM  linux/amd64 (default, the CI runner's) or linux/arm64, run emulated when it is not the host's
#     IMAGE     an image reference to test instead of building one (the release job passes its digest)
#
# Every container and the volume are removed on exit, whatever the outcome.
set -euo pipefail
cd "$(dirname "$0")/../.."

PLATFORM="${1:-linux/amd64}"
IMAGE="${2:-}"
RUN_ID="redirect-image-test-$$"
VOLUME="$RUN_ID-data"
WORK="$(mktemp -d)"
SLUG="image-test/landing"
TARGET="http://example.test/landing?from=image-test"
# Emulated platforms start slowly: generous bound, the health check itself polls every 2 s while starting
START_TIMEOUT=120

cleanup() {
  local status=$?
  if [ "$status" -ne 0 ]; then
    for name in "$RUN_ID-a" "$RUN_ID-b" "$RUN_ID-c"; do
      if docker container inspect "$name" >/dev/null 2>&1; then
        echo "--- logs of $name"
        docker logs --tail 50 "$name" 2>&1 || true
      fi
    done
  fi
  docker rm --force "$RUN_ID-a" "$RUN_ID-b" "$RUN_ID-c" >/dev/null 2>&1 || true
  docker volume rm --force "$VOLUME" >/dev/null 2>&1 || true
  rm -rf "$WORK"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

check() { # LABEL ACTUAL EXPECTED
  [ "$2" = "$3" ] || fail "$1: expected '$3', got '$2'"
  echo "ok   $1"
}

contains() { # LABEL TEXT NEEDLE
  case "$2" in *"$3"*) echo "ok   $1" ;; *) fail "$1: '$3' not found in: $2" ;; esac
}

# HTTP status of a request; the body goes to $WORK/body and the headers to $WORK/headers. No redirect followed.
request() {
  curl --silent --show-error --max-time 10 --output "$WORK/body" --dump-header "$WORK/headers" \
    --write-out '%{http_code}' "$@"
}

header() { # NAME: value of the header in the last response, without the trailing CR
  grep -i "^$1:" "$WORK/headers" | head -n 1 | cut -d ' ' -f 2- | tr -d '\r'
}

state() { # NAME: "status health", e.g. "running healthy" or "running starting"
  docker container inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' "$1"
}

# Start NAME from the image, hardened as recommended (read-only root, no capability), on the test volume,
# wait until Docker reports it healthy through the image's own HEALTHCHECK, and set BASE to its URL.
start() { # NAME [docker run options]
  local name="$1" waited=0
  shift
  docker run --detach --name "$name" --platform "$PLATFORM" --read-only --cap-drop ALL \
    --security-opt no-new-privileges --publish 127.0.0.1::8080 --volume "$VOLUME:/data" "$@" "$IMAGE" >/dev/null
  until [ "$(state "$name")" = "running healthy" ]; do
    case "$(state "$name")" in
      "running starting") ;;
      "running ") fail "$name: the image has no HEALTHCHECK" ;;
      *) fail "$name: not healthy ($(state "$name"))" ;;
    esac
    waited=$((waited + 1))
    [ "$waited" -le "$START_TIMEOUT" ] || fail "$name: not healthy after ${START_TIMEOUT}s"
    sleep 1
  done
  BASE="http://$(docker port "$name" 8080/tcp | head -n 1)"
  echo "ok   $name healthy after ${waited}s ($BASE)"
}

# docker stop sends SIGTERM: the app must shut down cleanly (exit code 0) well within the grace period
stop() { # NAME
  docker stop --time 20 "$1" >/dev/null
  check "$1 stops cleanly on SIGTERM (exit code)" "$(docker container inspect --format '{{.State.ExitCode}}' "$1")" 0
  contains "$1 logs its shutdown" "$(docker logs "$1" 2>&1)" "Redirect stopped"
}

if [ -z "$IMAGE" ]; then
  IMAGE="redirect:image-test-${PLATFORM##*/}"
  echo "Building $IMAGE for $PLATFORM"
  docker build --quiet --platform "$PLATFORM" --tag "$IMAGE" . >/dev/null
fi
echo "Testing $IMAGE on $PLATFORM"
docker volume create "$VOLUME" >/dev/null

# --- First run: a fresh volume --------------------------------------------------------------------------------
start "$RUN_ID-a"

check "GET /api/health" "$(request "$BASE/api/health")" 200
check "health body" "$(cat "$WORK/body")" '{"status":"ok"}'

# The server process itself (PID 1), not only the image's USER setting
check "server runs as uid/gid 10001" \
  "$(docker exec "$RUN_ID-a" python -c "
lines = dict(line.split(':', 1) for line in open('/proc/1/status'))
print(lines['Uid'].split()[0], lines['Gid'].split()[0])")" "10001 10001"

# What ships: the installed app and its locked dependencies; no installer, test, browser, lint or build tool
check "image contents" "$(docker exec "$RUN_ID-a" python -c "
import importlib.util, os, shutil, sys
import redirect
found = [m for m in ('pip', 'ensurepip', 'setuptools', 'wheel', 'pytest', 'httpx', 'playwright', 'ruff', 'mypy')
         if importlib.util.find_spec(m)]
found += [c for c in ('pip', 'pip3', 'uv', 'gcc', 'cc') if shutil.which(c)]
if not redirect.__file__.startswith(sys.prefix + '/lib/'):
    found.append('app not installed in the venv: ' + redirect.__file__)
stat = os.stat('/data')
if (stat.st_uid, stat.st_gid) != (10001, 10001):
    found.append(f'/data owned by {stat.st_uid}:{stat.st_gid}')
print(' '.join(found) or 'clean')")" clean

# The web fonts ship in the package and are served with their type, byte for byte
fonts=0
for font in redirect/static/fonts/*.woff2; do
  name="$(basename "$font")"
  check "GET /static/fonts/$name" "$(request "$BASE/static/fonts/$name")" 200
  check "$name content type" "$(header content-type)" font/woff2
  cmp -s "$WORK/body" "$font" || fail "$name: served bytes differ from the repository's"
  fonts=$((fonts + 1))
done
check "web fonts served" "$fonts" 4

check "GET / (the interface)" "$(request "$BASE/")" 200
contains "interface content type" "$(header content-type)" text/html

# Create a redirect through the API, then follow it. The body is built outside the command substitution:
# bash 3.2 (macOS) mangles escaped quotes nested in "$(...)".
body="{\"slug\": \"$SLUG\", \"name\": \"Image test\", \"target\": \"$TARGET\"}"
check "POST /api/redirects" "$(request --request POST --header 'Content-Type: application/json' \
  --data "$body" "$BASE/api/redirects")" 201
contains "public link built from the Host header" "$(cat "$WORK/body")" "\"public_url\":\"$BASE/redirect/$SLUG\""

check "GET /redirect/$SLUG" "$(request "$BASE/redirect/$SLUG")" 302
check "Location" "$(header location)" "$TARGET"
check "Cache-Control of the redirect" "$(header cache-control)" no-store

check "GET /redirect/image-test/unknown" "$(request "$BASE/redirect/image-test/unknown")" 404
contains "404 content type" "$(header content-type)" text/html

# Safe default: X-Forwarded-* from anything but the loopback is ignored, the link keeps the real scheme
check "GET /api/redirects, forwarded https" "$(request --header 'X-Forwarded-Proto: https' "$BASE/api/redirects")" 200
contains "forwarded headers ignored by default" "$(cat "$WORK/body")" "\"base_url\":\"$BASE\""

logs="$(docker logs "$RUN_ID-a" 2>&1)"
contains "one log line per redirect" "$logs" "redirect slug=$SLUG status=302"
case "$logs" in *Traceback*) fail "a traceback in the logs" ;; esac
stop "$RUN_ID-a"

# --- Second run: same volume, as after an upgrade, trusting every proxy -----------------------------------------
start "$RUN_ID-b" --env 'FORWARDED_ALLOW_IPS=*'
check "GET /redirect/$SLUG after a restart" "$(request "$BASE/redirect/$SLUG")" 302
check "Location after a restart" "$(header location)" "$TARGET"
request --header 'X-Forwarded-Proto: https' "$BASE/api/redirects" >/dev/null
contains "FORWARDED_ALLOW_IPS honoured" "$(cat "$WORK/body")" "\"base_url\":\"https://${BASE#http://}\""
stop "$RUN_ID-b"

# --- Third run: a user that cannot write to /data must get a clear error, not a crash loop ----------------------
docker run --detach --name "$RUN_ID-c" --platform "$PLATFORM" --read-only --user 12345:12345 \
  --volume "$VOLUME:/data" "$IMAGE" >/dev/null
waited=0
until [ "$(docker container inspect --format '{{.State.Status}}' "$RUN_ID-c")" = exited ]; do
  waited=$((waited + 1))
  [ "$waited" -le "$START_TIMEOUT" ] || fail "$RUN_ID-c: still running after ${START_TIMEOUT}s"
  sleep 1
done
[ "$(docker container inspect --format '{{.State.ExitCode}}' "$RUN_ID-c")" != 0 ] ||
  fail "$RUN_ID-c: exit code 0 without a writable /data"
contains "unwritable /data explained" "$(docker logs "$RUN_ID-c" 2>&1)" "is writable by this user"

echo "Image test passed: $IMAGE ($PLATFORM)"
