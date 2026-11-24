#!/bin/bash
# Chromaphore container entrypoint.
#
#   docker run ... <image>                         the browser, headless, CDP on port 9222
#   docker run ... <image> --fingerprint-seed=12345
#                                                  switches go to the browser
#   docker run ... <image> chromaphore --version   anything not starting with "-" runs as given
#
# Environment:
#   CHROMAPHORE_IDENTITY=off   no fingerprint identity; by default each profile folder gets one
#   CDP_PORT                   the port CDP answers on in the container (default 9222)
#   CDP_ALLOWED_HOSTS          dotted names clients may connect by, comma-separated
#                              (single-word names such as a compose service's always work)
#   CHROMAPHORE_USER_DATA_DIR  the profile folder (default ~/data; mount a volume there to keep it)
#   CHROMAPHORE_SANDBOX=1      keep the browser's sandbox on; the container must then be
#                              allowed user namespaces (README, Docker)
#   TZ                         the time zone the browser reports (default UTC)
set -euo pipefail

# A `docker stop` (tini signals the whole group, the browser included) is
# waited out from the first moment, so that the browser closes cleanly and its
# exit code becomes the container's. An exec below puts the signal back to its
# default for the program that takes over.
stopping=0
trap 'stopping=1' TERM INT

if [ "$#" -gt 0 ] && [ "${1#-}" = "$1" ]; then
  exec "$@"
fi

# The browser never runs as root: a container started with --user 0 goes on as
# the browser's user.
if [ "$(id -u)" = 0 ]; then
  export HOME=/home/chromaphore USER=chromaphore LOGNAME=chromaphore
  exec setpriv --reuid=chromaphore --regid=chromaphore --init-groups "$0" "$@"
fi

PORT="${CDP_PORT:-9222}"
DATA="${CHROMAPHORE_USER_DATA_DIR:-$HOME/data}"
if ! mkdir -p "$DATA" 2> /dev/null || ! [ -w "$DATA" ]; then
  echo "The profile folder $DATA is not writable by the browser's user (uid $(id -u)): chown the mounted folder to it." >&2
  exit 1
fi

# One container at a time on a profile. This lock is the system's, so it is
# gone as soon as its container's processes are, however they ended. Holding
# it, whatever the browser left in the folder to say "in use" comes from a
# container that was killed, and would stop this start: it is taken away.
exec 9> "$DATA/chromaphore-lock"
lock=0
flock -n -E 75 9 || lock=$?
if [ "$lock" = 75 ]; then
  echo "The profile folder $DATA is in use by another running container." >&2
  exit 1
fi
rm -f "$DATA/SingletonLock" "$DATA/SingletonSocket" "$DATA/SingletonCookie" "$DATA/DevToolsActivePort"
if [ "$stopping" = 1 ]; then exit 0; fi

# An identity of its own for each profile, unless the start says otherwise: a
# seed drawn once and kept in the profile folder, so that a kept profile stays
# the same machine and a fresh container is a new one. A --fingerprint-seed
# among the switches takes its place; --use-chromium-defaults and
# CHROMAPHORE_IDENTITY=off run without an identity (the server's real values).
identity=()
if [ "${CHROMAPHORE_IDENTITY:-profile}" != off ]; then
  given=0
  for arg in "$@"; do
    case "$arg" in
      --fingerprint-seed | --fingerprint-seed=* | --use-chromium-defaults) given=1 ;;
    esac
  done
  if [ "$given" = 0 ]; then
    seed=
    if [ -s "$DATA/chromaphore-seed" ]; then
      seed=$(head -n 1 "$DATA/chromaphore-seed" 2> /dev/null || true)
    fi
    case "$seed" in
      '' | *[!0-9]*)
        seed=$(($(od -An -N4 -tu4 /dev/urandom) & 0x7fffffff))
        echo "$seed" > "$DATA/chromaphore-seed"
        ;;
    esac
    identity=(--fingerprint-seed="$seed" --fingerprint-screen=auto --fingerprint-screen-position=auto)
  fi
fi

# --no-sandbox: a container usually cannot give the sandbox its namespaces
# (INSTALL.md, Servers). --disable-gpu --disable-software-rasterizer: no GPU, so
# no WebGL, as a Windows machine without a usable graphics card has it.
# --disable-dev-shm-usage: Docker's /dev/shm is 64 MB, too small for renderers.
# --remote-debugging-port=0: the browser picks a free loopback port and writes
# it to DevToolsActivePort; the forwarder below puts it on $PORT.
sandbox=(--no-sandbox)
if [ "${CHROMAPHORE_SANDBOX:-0}" = 1 ]; then sandbox=(); fi

/opt/chromaphore/chrome \
  --headless=new \
  "${sandbox[@]}" \
  --disable-gpu \
  --disable-software-rasterizer \
  --disable-dev-shm-usage \
  --no-first-run \
  --no-default-browser-check \
  --remote-debugging-port=0 \
  --user-data-dir="$DATA" \
  "${identity[@]}" \
  "$@" &
browser=$!

inner=
for _ in $(seq 1 300); do
  if [ -s "$DATA/DevToolsActivePort" ]; then
    inner=$(head -n 1 "$DATA/DevToolsActivePort" 2> /dev/null || true)
    case "$inner" in '' | *[!0-9]*) inner= ;; *) break ;; esac
  fi
  if [ "$stopping" = 1 ]; then inner=; break; fi
  kill -0 "$browser" 2> /dev/null || break
  sleep 0.1 || true  # a stop signal reaches the sleep too
done
if [ -z "$inner" ]; then
  set +e
  if [ "$stopping" = 1 ]; then
    wait "$browser"
    status=$?
    while [ "$status" -gt 128 ] && kill -0 "$browser" 2> /dev/null; do
      wait "$browser"
      status=$?
    done
    exit "$status"
  fi
  if kill -0 "$browser" 2> /dev/null; then
    echo "The browser did not open its DevTools port within 30 s." >&2
    kill "$browser"
    wait "$browser"
    exit 1
  fi
  # It ended by itself: --version, or a switch it refused.
  wait "$browser"
  exit $?
fi

python3 -I /usr/local/bin/chromaphore-cdp-proxy "$PORT" "$inner" &
forward=$!

# The container lives as long as both do. On `docker stop` the forwarder ends at
# once and the browser closes; the browser's exit code (0 after a clean close)
# is then the container's.
set +e
ended=
status=0
while [ -z "${ended:-}" ]; do
  wait -n -p ended "$browser" "$forward"
  status=$?
  if [ "$status" = 127 ]; then break; fi
done
if [ "${ended:-}" = "$browser" ]; then
  kill "$forward" 2> /dev/null
elif [ "$stopping" = 1 ]; then
  wait "$browser"
  status=$?
  while [ "$status" -gt 128 ] && kill -0 "$browser" 2> /dev/null; do
    wait "$browser"
    status=$?
  done
else
  echo "The CDP forwarder ended (exit $status); stopping the browser." >&2
  kill "$browser" 2> /dev/null
  wait "$browser"
  status=1
fi
exit "$status"
