#!/usr/bin/env python3
"""CDP forwarder for the Chromaphore container.

    cdp-proxy.py <listen port> <browser port>

In --headless=new the browser's DevTools server listens on 127.0.0.1 only, so
this listens on 0.0.0.0:<listen port> and forwards to it, like
`socat TCP-LISTEN:... TCP:...`, with one addition: other containers may reach
the browser by name.

The browser answers 500 "Host header is specified and is not an IP address or
localhost." to a request whose Host is a name. That is its guard against DNS
rebinding: a web page that reaches this port under a domain of its own would
otherwise read the list of open tabs. So a name is only helped past it when it
is one a web page cannot be served under through public DNS:

  - a single word, like a compose service or container name (`chromaphore`);
  - a name listed in CDP_ALLOWED_HOSTS (comma-separated), for the dotted names
    you use yourself, such as `host.docker.internal` or a Kubernetes service's
    `browser.namespace.svc.cluster.local`. List only names that no one else
    can point at this port.

For those the browser is shown Host: 127.0.0.1:<port>, and the /json replies
get the client's own Host back in webSocketDebuggerUrl and devtoolsFrontendUrl,
so the ws:// URL a client receives is the address it already reached. Such a
request is refused when it carries Origin, as every WebSocket and every
request other than a plain GET from a web page does and no CDP client's does,
and it must be plainly formed: one Host, no folded or bare-newline header
lines, a body only by Content-Length. Everything else (an IP address,
localhost, any other name, anything oddly formed) goes to the browser byte for
byte and gets the browser's own answer.
"""
import asyncio
import ipaddress
import json
import os
import re
import sys

LISTEN_PORT = int(sys.argv[1])
UPSTREAM_PORT = int(sys.argv[2])
STAND_IN = "127.0.0.1:%d" % LISTEN_PORT  # the Host the browser is shown instead of a name
MAX_HEAD = 64 * 1024
MAX_BODY = 1024 * 1024  # of a request by name; CDP's HTTP endpoints take none
HEAD_TIMEOUT = 15   # seconds a client has to send a request head, or its body
IDLE_TIMEOUT = 300  # seconds a connection by name may rest between requests
CLOSE_GRACE = 2     # seconds one direction gets to finish once the other has ended
URL_FIELDS = ("webSocketDebuggerUrl", "devtoolsFrontendUrl")
HOST = re.compile(r"(?P<name>[a-z0-9]([a-z0-9._-]*[a-z0-9])?)(:[0-9]{1,5})?\Z")
SINGLE_WORD = re.compile(r"[a-z0-9]([a-z0-9_-]{0,61}[a-z0-9])?\Z")
EXTRA_NAMES = frozenset(
    name for name in (n.strip().rstrip(".").lower()
                      for n in os.environ.get("CDP_ALLOWED_HOSTS", "").split(","))
    if name)


class Refuse(Exception):
    """A request by name that is answered here, with this status line."""


def browser_accepts(name):
    """What the browser answers by itself: an IP literal, localhost, *.localhost."""
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def helped_name(headers):
    """The name in the request's one Host header when it is a name this
    forwarder helps past the browser's check, else None."""
    hosts = [v for k, v in headers if k.lower() == "host"]
    if len(hosts) != 1:
        return None
    m = HOST.match(hosts[0].lower())
    if not m:
        return None
    name = m.group("name")
    if browser_accepts(name):
        return None
    return name if SINGLE_WORD.match(name) or name in EXTRA_NAMES else None


async def read_head(reader, timeout=None):
    """One HTTP message head as (first line, [(name, value)], raw bytes), or
    None at the end of the stream."""
    try:
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
    except asyncio.IncompleteReadError:
        return None
    except asyncio.LimitOverrunError:
        raise ValueError("head too large")
    lines = raw[:-4].decode("latin-1").split("\r\n")
    headers = []
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers.append((name.strip(), value.strip()))
    return lines[0], headers, raw


def plainly_formed(head):
    """Whether the browser is certain to read this head as it is read here: no
    bare CR or LF, and every header line `name: value` from its first column."""
    lines = head[2][:-4].decode("latin-1").split("\r\n")
    if any("\r" in line or "\n" in line for line in lines):
        return False
    return all(":" in line and line[:1] not in (" ", "\t", ":") for line in lines[1:])


def get(headers, name):
    for k, v in headers:
        if k.lower() == name:
            return v
    return None


def build(first, headers):
    out = first + "\r\n" + "".join("%s: %s\r\n" % kv for kv in headers) + "\r\n"
    return out.encode("latin-1")


async def pipe(reader, writer, pass_end=False):
    """Bytes one way until the reader ends."""
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
        if pass_end and writer.can_write_eof():
            writer.write_eof()
    except (ConnectionError, OSError):
        pass


async def splice(c_reader, c_writer, u_reader, u_writer):
    """Raw bytes both ways. A client that has stopped sending may still be
    waiting for its answer, and the browser drops an answer it owes once it sees
    the client's end: so that end is not passed on, and the browser's direction
    gets a moment to finish before both sides are closed."""
    tasks = [asyncio.ensure_future(pipe(c_reader, u_writer)),
             asyncio.ensure_future(pipe(u_reader, c_writer, pass_end=True))]
    _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    if pending:
        _, pending = await asyncio.wait(pending, timeout=CLOSE_GRACE)
    for task in pending:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def fix_urls(value, client_host):
    """Put the client's Host back in place of the stand-in in the URL fields."""
    if isinstance(value, list):
        return [fix_urls(v, client_host) for v in value]
    if isinstance(value, dict):
        for field in URL_FIELDS:
            if isinstance(value.get(field), str):
                value[field] = (value[field]
                                .replace("ws://%s/" % STAND_IN, "ws://%s/" % client_host)
                                .replace("ws=%s/" % STAND_IN, "ws=%s/" % client_host))
    return value


def body_length(headers):
    """The length of a request-by-name's body, or Refuse."""
    if get(headers, "transfer-encoding") is not None:
        raise Refuse("400 Bad Request")
    lengths = [v for k, v in headers if k.lower() == "content-length"]
    if not lengths:
        return 0
    if len(lengths) > 1 or not re.match(r"[0-9]{1,9}\Z", lengths[0]):
        raise Refuse("400 Bad Request")
    if int(lengths[0]) > MAX_BODY:
        raise Refuse("413 Content Too Large")
    return int(lengths[0])


def wants_close(first, headers):
    connection = (get(headers, "connection") or "").lower()
    if "close" in connection:
        return True
    return first.rstrip().endswith("HTTP/1.0") and "keep-alive" not in connection


async def relay_by_name(head, c_reader, c_writer, u_reader, u_writer):
    """Request by request, with Host replaced and the /json replies fixed up."""
    while head is not None:
        first, headers, _ = head
        if not plainly_formed(head):
            raise Refuse("400 Bad Request")
        if helped_name(headers) is None or get(headers, "origin") is not None \
                or get(headers, "sec-fetch-site") is not None:
            # A web page's WebSocket or non-GET request, or a later request on
            # the connection under another name.
            raise Refuse("403 Forbidden")
        client_host = get(headers, "host")
        body_len = body_length(headers)
        headers = [(k, STAND_IN if k.lower() == "host" else v) for k, v in headers]
        u_writer.write(build(first, headers))
        if body_len:
            u_writer.write(await asyncio.wait_for(c_reader.readexactly(body_len), HEAD_TIMEOUT))
        await u_writer.drain()

        resp = await read_head(u_reader)
        if resp is None:
            return
        status_line, resp_headers, _ = resp
        length = get(resp_headers, "content-length")
        if status_line.split(" ")[1:2] == ["101"]:  # a WebSocket from here on
            c_writer.write(build(status_line, resp_headers))
            await c_writer.drain()
            await splice(c_reader, c_writer, u_reader, u_writer)
            return
        if length is None:  # not something the browser sends: pass it on and end
            c_writer.write(build(status_line, resp_headers))
            await c_writer.drain()
            await pipe(u_reader, c_writer, pass_end=True)
            return
        body = await u_reader.readexactly(int(length))
        ctype = (get(resp_headers, "content-type") or "").lower()
        path = first.split(" ")[1] if first.count(" ") >= 2 else ""
        if path.startswith("/json") and "json" in ctype:
            try:
                body = json.dumps(fix_urls(json.loads(body), client_host),
                                  indent=3).encode("utf-8")
            except ValueError:
                pass
            resp_headers = [(k, str(len(body)) if k.lower() == "content-length" else v)
                            for k, v in resp_headers]
        c_writer.write(build(status_line, resp_headers) + body)
        await c_writer.drain()
        if wants_close(first, headers) or wants_close("", resp_headers):
            return
        head = await read_head(c_reader, IDLE_TIMEOUT)


async def handle(c_reader, c_writer):
    u_writer = None
    try:
        head = await read_head(c_reader, HEAD_TIMEOUT)
        if head is None:
            return
        u_reader, u_writer = await asyncio.open_connection("127.0.0.1", UPSTREAM_PORT)
        if helped_name(head[1]) is not None and plainly_formed(head):
            await relay_by_name(head, c_reader, c_writer, u_reader, u_writer)
        else:
            # The browser's own business: forward as is, like socat.
            u_writer.write(head[2])
            await u_writer.drain()
            await splice(c_reader, c_writer, u_reader, u_writer)
    except Refuse as refusal:
        try:
            c_writer.write(("HTTP/1.1 %s\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                            % refusal).encode("latin-1"))
            await c_writer.drain()
        except (ConnectionError, OSError):
            pass
    except (ConnectionError, OSError, ValueError, asyncio.IncompleteReadError,
            asyncio.TimeoutError):
        pass
    finally:
        for w in (c_writer, u_writer):
            if w is not None:
                try:
                    w.close()
                except Exception:
                    pass


async def main():
    server = await asyncio.start_server(handle, "0.0.0.0", LISTEN_PORT,
                                        reuse_address=True, limit=MAX_HEAD)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
