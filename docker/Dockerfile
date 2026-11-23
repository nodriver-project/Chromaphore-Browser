# syntax=docker/dockerfile:1
#
# Official Chromaphore image: headless, CDP on port 9222.
#
#   docker build -t chromaphore .
#   docker run -d --name chromaphore -p 127.0.0.1:9222:9222 chromaphore
#   curl http://127.0.0.1:9222/json/version
#
# The browser is the published Linux release tarball, unmodified. The build
# downloads it from the GitHub release and checks its SHA-256 before unpacking.
# Defaults are the v2.0.0 release; the workflow passes the release being built.

ARG UBUNTU_VERSION=24.04

# ---- 1. fetch and verify the release ---------------------------------------
FROM ubuntu:${UBUNTU_VERSION} AS fetch

ARG CHROMAPHORE_VERSION=2.0.0
ARG CHROMIUM_VERSION=156.0.8078.47
ARG CHROMAPHORE_URL=https://github.com/nodriver-project/Chromaphore-Browser/releases/download/v${CHROMAPHORE_VERSION}/Chromaphore-${CHROMAPHORE_VERSION}-chromium${CHROMIUM_VERSION}-linux-x64.tar.xz
ARG CHROMAPHORE_SHA256=ed2d01a83850f4740b24b12b744c8bc912f0b22f2e61d70449f288726503d68d

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl xz-utils \
 && rm -rf /var/lib/apt/lists/*

RUN set -eu; \
    curl -fsSL --retry 3 --retry-delay 5 -o /tmp/chromaphore.tar.xz "${CHROMAPHORE_URL}"; \
    echo "${CHROMAPHORE_SHA256}  /tmp/chromaphore.tar.xz" | sha256sum -c -; \
    tar -xJf /tmp/chromaphore.tar.xz -C /opt; \
    rm /tmp/chromaphore.tar.xz; \
    test -x /opt/chromaphore/chrome

# ---- 2. runtime -------------------------------------------------------------
FROM ubuntu:${UBUNTU_VERSION}

ARG CHROMAPHORE_VERSION=2.0.0
ARG CHROMIUM_VERSION=156.0.8078.47

LABEL org.opencontainers.image.title="Chromaphore" \
      org.opencontainers.image.description="Chromaphore headless browser with CDP on port 9222" \
      org.opencontainers.image.source="https://github.com/nodriver-project/Chromaphore-Browser" \
      org.opencontainers.image.url="https://github.com/nodriver-project/Chromaphore-Browser" \
      org.opencontainers.image.version="${CHROMAPHORE_VERSION}" \
      org.opencontainers.image.licenses="LicenseRef-Chromaphore-Binary" \
      io.chromaphore.chromium-version="${CHROMIUM_VERSION}"

# The browser first, as its own layer: changing the packages below does not
# re-copy its 2.9 GB.
COPY --link --from=fetch /opt/chromaphore /opt/chromaphore

# Libraries: the list in the release's INSTALL.md (minimal Ubuntu 24.04).
# socat: forwards 0.0.0.0:9222 to the browser's loopback-only CDP port.
# tini: PID 1, so `docker stop` reaches the browser and the forwarder.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64 \
      libcairo2 libcups2t64 libdbus-1-3 libdrm2 libexpat1 libgbm1 \
      libglib2.0-0t64 libnspr4 libnss3 libpango-1.0-0 libudev1 libx11-6 \
      libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 libxkbcommon0 \
      libxrandr2 \
      socat tini \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb \
 && { userdel --remove ubuntu 2>/dev/null || true; } \
 && useradd --create-home --uid 1000 --shell /bin/bash chromaphore

COPY --chmod=755 entrypoint.sh /usr/local/bin/chromaphore-entrypoint

# C.UTF-8 so file paths with non-ASCII letters work (INSTALL.md, Locale).
# TZ is passed through to the browser: docker run -e TZ=America/New_York ...
ENV LC_ALL=C.UTF-8 \
    CDP_PORT=9222

USER chromaphore
WORKDIR /home/chromaphore
EXPOSE 9222

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD bash -c 'exec 3<>/dev/tcp/127.0.0.1/${CDP_PORT} && printf "GET /json/version HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n" >&3 && grep -q webSocketDebuggerUrl <&3'

ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/usr/local/bin/chromaphore-entrypoint"]
CMD []
