#!/usr/bin/env python3
"""CDP forwarder for the Chromaphore container.

Listens on 0.0.0.0:<port> and forwards to the browser's loopback-only CDP
server on 127.0.0.1:<inner port>, like `socat TCP-LISTEN:... TCP:...`, with one
difference: it lets clients use a hostname.

The browser's DevTools HTTP server answers 500 "Host header is specified and
is not an IP address or localhost." to any HTTP or WebSocket request whose Host
is a name (e.g. `chromaphore:9222`, a docker compose service name). For such a
connection this forwarder sends Host: 127.0.0.1:<port> to the browser instead,
and in the /json replies it puts the client's own Host back into
webSocketDebuggerUrl and devtoolsFrontendUrl, so the ws:// URL the client gets
is the address it already reached. Connections whose Host is an IP address or
localhost are passed through untouched, byte for byte.

    cdp-proxy.py <listen port> <browser port>
"""
import asyncio
import ipaddress
import json
import sys

LISTEN_PORT = int(sys.argv[1])
UPSTREAM_PORT = int(sys.argv[2])
STAND_IN = "127.0.0.1:%d" % LISTEN_PORT  # the Host the browser is shown instead
MAX_HEAD = 64 * 1024
URL_FIELDS = ("webSocketDebuggerUrl", "devtoolsFrontendUrl")


def host_is_allowed(host):
    """What the browser accepts: an IP literal, localhost or *.localhost."""
    if host.startswith("["):  # [IPv6]:port
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    host = host.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


async def read_head(reader):
    """One HTTP message head as (first line, [(name, value)], raw bytes), or
    None at EOF."""
    try:
        raw = await reader.readuntil(b"\r\n\r\n")
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


def get(headers, name):
    for k, v in headers:
        if k.lower() == name:
            return v
    return None


def build(first, headers):
    out = first + "\r\n" + "".join("%s: %s\r\n" % kv for kv in headers) + "\r\n"
    return out.encode("latin-1")


async def pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def splice(c_reader, c_writer, u_reader, u_writer):
    await asyncio.gather(pipe(c_reader, u_writer), pipe(u_reader, c_writer))


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


async def relay_rewritten(head, c_reader, c_writer, u_reader, u_writer):
    """Request by request, with Host replaced and /json replies fixed up."""
    while head is not None:
        first, headers, _ = head
        if get(headers, "origin") is not None or get(headers, "sec-fetch-site") is not None:
            # Web pages send these and CDP clients don't. The browser refuses
            # name Hosts to keep pages that reach this port under a name of
            # their own (DNS rebinding) out, so those stay refused here.
            c_writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n"
                           b"Connection: close\r\n\r\n")
            await c_writer.drain()
            return
        client_host = get(headers, "host")
        headers = [(k, STAND_IN if k.lower() == "host" else v) for k, v in headers]
        body_len = int(get(headers, "content-length") or 0)
        u_writer.write(build(first, headers))
        if body_len:
            u_writer.write(await c_reader.readexactly(body_len))
        await u_writer.drain()

        resp = await read_head(u_reader)
        if resp is None:
            return
        status_line, resp_headers, _ = resp
        if status_line.split(" ")[1:2] == ["101"]:  # WebSocket: raw from here
            c_writer.write(build(status_line, resp_headers))
            await c_writer.drain()
            await splice(c_reader, c_writer, u_reader, u_writer)
            return
        length = get(resp_headers, "content-length")
        if length is None:  # not something the browser sends; stop parsing
            c_writer.write(build(status_line, resp_headers))
            await c_writer.drain()
            await splice(c_reader, c_writer, u_reader, u_writer)
            return
        body = await u_reader.readexactly(int(length))
        ctype = (get(resp_headers, "content-type") or "").lower()
        path = first.split(" ")[1] if " " in first else ""
        if path.startswith("/json") and "json" in ctype and client_host:
            try:
                body = json.dumps(fix_urls(json.loads(body), client_host),
                                  indent=3).encode("utf-8")
            except ValueError:
                pass
            resp_headers = [(k, str(len(body)) if k.lower() == "content-length" else v)
                            for k, v in resp_headers]
        c_writer.write(build(status_line, resp_headers) + body)
        await c_writer.drain()
        head = await read_head(c_reader)


async def handle(c_reader, c_writer):
    u_writer = None
    try:
        u_reader, u_writer = await asyncio.open_connection("127.0.0.1", UPSTREAM_PORT)
        head = await read_head(c_reader)
        if head is None:
            return
        host = get(head[1], "host")
        if host is None or host_is_allowed(host):
            # What the browser accepts anyway: forward as is, like socat.
            u_writer.write(head[2])
            await u_writer.drain()
            await splice(c_reader, c_writer, u_reader, u_writer)
        else:
            await relay_rewritten(head, c_reader, c_writer, u_reader, u_writer)
    except (ConnectionError, OSError, ValueError, asyncio.IncompleteReadError):
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
