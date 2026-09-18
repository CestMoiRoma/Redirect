# The Redirect application image. Not the CI image (test/ci/Dockerfile): no test, browser or lint tool ships here.
# Build context: the repository root, filtered by .dockerignore down to the app package and its lock.
#   docker build -t redirect:local .                                (just image)
#   docker buildx build --platform linux/amd64,linux/arm64 .        (release workflow)

# Python 3.12, the version the app targets (.python-version), pinned by digest (multi-platform index)
ARG PYTHON_IMAGE=python:3.12-slim-trixie@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

# --- Build stage: a virtual environment holding the app and its locked dependencies, nothing else ----------
FROM ${PYTHON_IMAGE} AS build

# Build backend of pyproject.toml, pinned with its checksum like every other input (build stage only)
ARG SETUPTOOLS_VERSION=84.0.0
ARG SETUPTOOLS_SHA256=51a52592b3b99e102b609654876bd65f19f999935166d1352678931132b0c670

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PIP_ROOT_USER_ACTION=ignore PYTHONDONTWRITEBYTECODE=1

# Dependencies first: the locked closure (requirements.lock, exported from uv.lock), wheels only, every file
# checked against its hash. In hash-checking mode pip rejects any dependency the lock does not list, so an
# incomplete lock fails the build instead of resolving a new version. The venv gets no pip of its own.
RUN python -m venv --without-pip /venv
COPY requirements.lock /tmp/requirements.lock
RUN pip --python /venv/bin/python install --require-hashes --only-binary=:all: -r /tmp/requirements.lock

# Then the app itself: a wheel built offline, installed offline without dependencies. `pip check` fails the
# build if the app needs anything the lock did not bring.
RUN printf 'setuptools==%s --hash=sha256:%s\n' "$SETUPTOOLS_VERSION" "$SETUPTOOLS_SHA256" > /tmp/build.lock \
 && pip install --require-hashes --only-binary=:all: -r /tmp/build.lock
COPY pyproject.toml README.md /src/
COPY redirect /src/redirect
RUN pip wheel --no-index --no-deps --no-build-isolation --wheel-dir /tmp/wheels /src \
 && pip --python /venv/bin/python install --no-index --no-deps /tmp/wheels/redirect-*.whl \
 && pip --python /venv/bin/python check

# --- Final image ---------------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE}

LABEL org.opencontainers.image.title="Redirect" \
      org.opencontainers.image.description="Fixed public links whose target can change at any time." \
      org.opencontainers.image.source="https://github.com/CestMoiRoma/Redirect"

# No package installer at run time: pip and its bootstrap copy go. A fixed, unprivileged user owns /data
# and nothing else; the app only ever writes to DATA_DIR.
RUN rm -rf /usr/local/bin/pip* /usr/local/lib/python3.12/site-packages/pip* /usr/local/lib/python3.12/ensurepip \
 && groupadd --system --gid 10001 redirect \
 && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
      --shell /usr/sbin/nologin redirect \
 && install -d -o 10001 -g 10001 -m 0750 /data

COPY --from=build /venv /venv

# Health check: GET /api/health on the local port, standard library only, short timeout, proxies ignored
COPY <<'EOF' /app/healthcheck.py
"""Container health check: exit 0 when GET /api/health answers 200, 1 otherwise."""

import os
import sys
import urllib.request

url = f"http://127.0.0.1:{os.environ.get('PORT', '').strip() or '8080'}/api/health"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with opener.open(url, timeout=3) as response:
        sys.exit(0 if response.status == 200 else 1)
except Exception as exc:  # 503, refused connection, timeout
    sys.exit(f"unhealthy: {exc}")
EOF

# PORT, DATA_DIR and LOG_LEVEL are the app's defaults, spelled out. PUBLIC_BASE_URL stays unset: links then
# follow the Host header. FORWARDED_ALLOW_IPS is read by uvicorn: X-Forwarded-* headers are trusted from
# these addresses only. The default is uvicorn's (loopback only); behind a reverse proxy, set the proxy's address.
ENV PATH=/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PORT=8080 DATA_DIR=/data LOG_LEVEL=INFO FORWARDED_ALLOW_IPS=127.0.0.1,::1

WORKDIR /app
USER 10001:10001
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --start-interval=2s --retries=3 \
  CMD ["python", "/app/healthcheck.py"]

# uvicorn on $PORT, no reload, proxy headers honoured from FORWARDED_ALLOW_IPS only (redirect/__main__.py)
CMD ["python", "-m", "redirect"]
