#!/bin/sh
# Chromaphore container entrypoint.
#
#   docker run ... chromaphore                          headless browser, CDP on 9222
#   docker run ... chromaphore --fingerprint-seed=12345 extra browser switches are appended
#   docker run ... chromaphore bash                     anything not starting with "-" runs as-is
#
# Environment:
#   CDP_PORT                 port CDP answers on in the container (default 9222)
#   CHROMAPHORE_USER_DATA_DIR  profile folder (default ~/data; mount a volume here to keep it)
#   TZ                       time zone the browser reports (default UTC)
set -e

if [ "$#" -gt 0 ] && [ "${1#-}" = "$1" ]; then
  exec "$@"
fi

PORT="${CDP_PORT:-9222}"
INNER=$((PORT + 1))
DATA="${CHROMAPHORE_USER_DATA_DIR:-$HOME/data}"
mkdir -p "$DATA"

# In --headless=new the browser's CDP server listens on 127.0.0.1 only
# (--remote-debugging-address=0.0.0.0 is ignored), so a published port cannot
# reach it; cdp-proxy.py forwards the container's PORT to it. Unlike a plain
# TCP forwarder it also accepts a hostname in the Host header (the browser
# refuses one), so other containers can use http://<service name>:9222.
python3 -I /usr/local/bin/chromaphore-cdp-proxy "${PORT}" "${INNER}" &

# Flags: --no-sandbox (containers, INSTALL.md); --disable-gpu and
# --disable-software-rasterizer (no GPU, INSTALL.md); --disable-dev-shm-usage
# (Docker's /dev/shm is 64 MB by default, too small for page renderers).
exec /opt/chromaphore/chromaphore \
  --headless=new \
  --no-sandbox \
  --disable-gpu \
  --disable-software-rasterizer \
  --disable-dev-shm-usage \
  --no-first-run \
  --no-default-browser-check \
  --remote-debugging-port="${INNER}" \
  --user-data-dir="${DATA}" \
  "$@"
