# syntax=docker/dockerfile:1
#
# The Chromaphore image: the released Linux build, headless, with CDP on port 9222.
#
#   docker build -t chromaphore docker
#   docker run -d --name chromaphore -p 127.0.0.1:9222:9222 chromaphore
#   curl http://127.0.0.1:9222/json/version
#
# The browser is a release's Linux tarball: the build downloads it from the
# Releases page and checks its SHA-256 before unpacking; no file of it is
# changed (only write permission for others is taken off its folders). The
# defaults are release 2.0.0; the workflow passes the release it builds.

ARG UBUNTU_VERSION=24.04

# ---- 1. fetch and verify the release ---------------------------------------
FROM ubuntu:${UBUNTU_VERSION} AS fetch

ARG CHROMAPHORE_VERSION=2.0.0
ARG CHROMIUM_VERSION=156.0.8078.47
ARG CHROMAPHORE_URL=https://github.com/nodriver-project/Chromaphore-Browser/releases/download/v${CHROMAPHORE_VERSION}/Chromaphore-${CHROMAPHORE_VERSION}-chromium${CHROMIUM_VERSION}-linux-x64.tar.xz
ARG CHROMAPHORE_SHA256=ed2d01a83850f4740b24b12b744c8bc912f0b22f2e61d70449f288726503d68d

# The release is built for x64 only. On another machine, build and run with
# --platform linux/amd64 (emulated, and slow).
ARG TARGETARCH
RUN [ "${TARGETARCH}" = amd64 ] || { echo "Chromaphore has no ${TARGETARCH} build: add --platform linux/amd64." >&2; exit 1; }

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl xz-utils \
 && rm -rf /var/lib/apt/lists/*

RUN set -eu; \
    curl -fsSL --retry 3 --retry-delay 5 -o /tmp/chromaphore.tar.xz "${CHROMAPHORE_URL}"; \
    echo "${CHROMAPHORE_SHA256}  /tmp/chromaphore.tar.xz" | sha256sum -c -; \
    tar -xJf /tmp/chromaphore.tar.xz -C /opt --no-same-owner; \
    rm /tmp/chromaphore.tar.xz; \
    chmod -R go-w /opt/chromaphore; \
    test -x /opt/chromaphore/chrome

# ---- 2. runtime -------------------------------------------------------------
FROM ubuntu:${UBUNTU_VERSION}

ARG CHROMAPHORE_VERSION=2.0.0
ARG CHROMIUM_VERSION=156.0.8078.47

LABEL org.opencontainers.image.title="Chromaphore" \
      org.opencontainers.image.description="Chromaphore, headless, with CDP on port 9222" \
      org.opencontainers.image.source="https://github.com/nodriver-project/Chromaphore-Browser" \
      org.opencontainers.image.url="https://github.com/nodriver-project/Chromaphore-Browser" \
      org.opencontainers.image.version="${CHROMAPHORE_VERSION}" \
      org.opencontainers.image.licenses="LicenseRef-Chromaphore-Binary" \
      io.chromaphore.chromium-version="${CHROMIUM_VERSION}"

# The browser first, as its own layer: changing what follows does not copy its
# 2.7 GB again.
COPY --link --from=fetch /opt/chromaphore /opt/chromaphore

# The libraries of the release's INSTALL.md (a minimal Ubuntu 24.04).
# python3 runs cdp-proxy.py; tini is PID 1, so `docker stop` reaches both
# processes.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64 \
      libcairo2 libcups2t64 libdbus-1-3 libdrm2 libexpat1 libgbm1 \
      libglib2.0-0t64 libnspr4 libnss3 libpango-1.0-0 libudev1 libx11-6 \
      libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 libxkbcommon0 \
      libxrandr2 \
      python3 tini \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb \
 && { userdel --remove ubuntu 2>/dev/null || true; } \
 && useradd --create-home --uid 1000 --shell /bin/bash chromaphore \
 && ln -s /opt/chromaphore/chrome /usr/local/bin/chromaphore

COPY --chmod=755 entrypoint.sh /usr/local/bin/chromaphore-entrypoint
COPY --chmod=755 cdp-proxy.py /usr/local/bin/chromaphore-cdp-proxy

# C.UTF-8 so that file paths with letters outside ASCII work (INSTALL.md,
# Locale). TZ goes through to the browser: docker run -e TZ=America/New_York.
ENV LC_ALL=C.UTF-8 \
    CDP_PORT=9222

USER chromaphore
WORKDIR /home/chromaphore

# The profile folder exists in the image so that a named volume mounted there
# starts out owned by the browser's user (an empty mount point would be root's).
RUN mkdir /home/chromaphore/data

# A container always starts as on its first launch, when the browser does not
# yet have the list of versions the stable channel serves and reports the
# build's own (INSTALL.md, First launch). So the image carries the list: one
# headless start that waits for it. A long-lived container refreshes it daily.
# No list comes once the stable channel has left this major version behind.
RUN /opt/chromaphore/chrome --headless=new --no-sandbox --disable-gpu --no-first-run \
      --user-data-dir=/tmp/prime about:blank > /dev/null 2>&1 & \
    pid=$!; \
    for i in $(seq 1 30); do \
      [ -s "$HOME/.config/Served Chrome Releases" ] && break; sleep 1; \
    done; \
    kill $pid 2> /dev/null; wait $pid 2> /dev/null; \
    rm -rf /tmp/prime "$HOME/.config/chromium" "$HOME/.cache" "$HOME/.local" "$HOME/.pki"; \
    if [ -s "$HOME/.config/Served Chrome Releases" ]; then \
      echo "release list: $(cat "$HOME/.config/Served Chrome Releases")"; \
    else \
      echo "no release list: the browser reports the build's own version"; \
    fi

EXPOSE 9222

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --start-interval=2s --retries=3 \
  CMD python3 -I -c "import os, urllib.request; urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:' + (os.environ.get('CDP_PORT') or '9222') + '/json/version', timeout=4)"

ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/usr/local/bin/chromaphore-entrypoint"]
CMD []
