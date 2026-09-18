# The one pinned image for lint, tests and GUI goldens. Not the app image: nothing here ships.
# Build context: test/  (docker build --platform linux/amd64 -f test/ci/Dockerfile test)
# Always built and run for linux/amd64, the CI runner's platform, emulated on ARM hosts.

# prettier needs Node: installed in the official Node image, pinned by digest, then copied into the Python image
FROM node:22-trixie-slim@sha256:7b8a0c89c54499bee567618f96578e1a12a800f062fbdbfd1fb6a443fa6f6284 AS node
ARG PRETTIER_VERSION=3.9.8
RUN npm install -g --no-audit --no-fund "prettier@${PRETTIER_VERSION}" && npm cache clean --force

# Python 3.12, the version the app targets (.python-version)
FROM python:3.12-slim-trixie@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

ARG DEBIAN_SNAPSHOT=20260915T000000Z
ARG TREEFMT_VERSION=2.6.0
ARG TREEFMT_SHA256=2deff4c0f381d560784c5632cf5860da712891ccd9185306e73d8ba37778ab7f

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    RUFF_CACHE_DIR=/tmp/ruff-cache MYPY_CACHE_DIR=/tmp/mypy-cache LANG=C.UTF-8 TZ=UTC PATH=/venv/bin:$PATH

# OS packages from a fixed snapshot: same versions on every build
RUN rm -f /etc/apt/sources.list.d/debian.sources \
 && printf '%s\n' \
    "deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT} trixie main" \
    "deb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT} trixie-security main" \
    > /etc/apt/sources.list \
 && apt-get -o Acquire::Retries=5 update \
 && apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
    ca-certificates curl git libstdc++6 \
    # Chromium headless shell libraries (Playwright's list for debian13, without its font packages)
    libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64 libcairo2 libcups2t64 libdbus-1-3 \
    libdrm2 libgbm1 libglib2.0-0t64 libnspr4 libnss3 libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 \
    libxdamage1 libxext6 libxfixes3 libxkbcommon0 libxrandr2 libfontconfig1 libfreetype6 \
    # Fonts: the UI ships its own web fonts; DejaVu is the only system fallback
    fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/* \
 # The repository is bind-mounted (or checked out by CI) with an owner that differs from the running user
 && git config --system --add safe.directory '*'

# Node + prettier from the first stage, then treefmt (checksum verified)
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/prettier /usr/local/lib/node_modules/prettier
RUN ln -s ../lib/node_modules/prettier/bin/prettier.cjs /usr/local/bin/prettier \
 && prettier --version \
 && curl -fsSL -o /tmp/treefmt.tgz \
      "https://github.com/numtide/treefmt/releases/download/v${TREEFMT_VERSION}/treefmt_${TREEFMT_VERSION}_linux_amd64.tar.gz" \
 && echo "${TREEFMT_SHA256}  /tmp/treefmt.tgz" | sha256sum -c - \
 && tar -xzf /tmp/treefmt.tgz -C /usr/local/bin treefmt && rm /tmp/treefmt.tgz \
 && treefmt --version

# Python: lock first (pinned closure), then the requirements offline (fails if the lock misses something)
RUN python3 -m venv /venv
COPY ci/requirements.lock ci/requirements.txt /tmp/ci/
COPY redirect/requirements-test.txt /tmp/redirect/requirements-test.txt
RUN pip install --no-cache-dir -r /tmp/ci/requirements.lock \
 && pip install --no-cache-dir --no-index -r /tmp/ci/requirements.txt \
 && playwright install --only-shell chromium \
 # Fails the build if a system library is missing
 && python -c "from playwright.sync_api import sync_playwright; pw = sync_playwright().start(); b = pw.chromium.launch(); print('chromium', b.version); b.close(); pw.stop()"

# fontconfig rules for screenshots (see the file)
COPY ci/fonts.conf /etc/fonts/conf.d/99-screenshots.conf
RUN fc-cache -f

WORKDIR /repo
