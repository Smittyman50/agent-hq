"""The egress proxy (deploy/egress/egress_proxy.py): rules per workbench, sign-in, relaying."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

PROXY = Path(__file__).resolve().parents[2] / "deploy" / "egress" / "egress_proxy.py"
_spec = importlib.util.spec_from_file_location("egress_proxy", PROXY)
eg = importlib.util.module_from_spec(_spec)
sys.modules["egress_proxy"] = eg
_spec.loader.exec_module(eg)


def test_rules_parse():
    r = eg.parse_rule("pypi.org")
    assert (r.kind, r.host, r.ports) == ("host", "pypi.org", frozenset({80, 443}))
    assert eg.parse_rule("*.github.com").kind == "wildcard"
    assert eg.parse_rule("gitgard.home.arpa:3000").ports == frozenset({3000})
    assert eg.parse_rule("gitgard.home.arpa:*").ports is None
    assert eg.parse_rule("192.168.10.0/24:443").kind == "net"
    assert eg.parse_rule("192.168.10.5").kind == "ip"
    assert eg.parse_rule("[fd00::1]:8080").ports == frozenset({8080})
    assert eg.parse_rule("*:443").kind == "any"
    for bad in ("", "pypi.org:99999", "pypi.org:http", "bad host!", "10.0.0.0/33", "a..b"):
        with pytest.raises(ValueError):
            eg.parse_rule(bad)


def test_decide():
    who = eg.Identity("workbench-1", eg.parse_rules(
        ["pypi.org:443", "*.github.com", "gitgard.home.arpa:3000", "192.168.10.0/24:443"]))
    assert eg.decide(who, "pypi.org", 443, ["1.1.1.1"]) == ["1.1.1.1"]
    assert eg.decide(who, "pypi.org", 80, ["1.1.1.1"]) is None
    assert eg.decide(who, "api.github.com", 443, ["2.2.2.2"]) == ["2.2.2.2"]
    assert eg.decide(who, "github.com", 80, ["2.2.2.2"]) == ["2.2.2.2"]
    assert eg.decide(who, "evilgithub.com", 443, ["2.2.2.2"]) is None
    assert eg.decide(who, "gitgard.home.arpa", 3000, ["192.168.1.9"]) == ["192.168.1.9"]
    assert eg.decide(who, "gitgard.home.arpa", 443, ["192.168.1.9"]) is None
    # A network rule lets names that resolve into it through, to those addresses only.
    assert eg.decide(who, "grafana.home.arpa", 443, ["192.168.10.7", "8.8.8.8"]) == ["192.168.10.7"]
    assert eg.decide(who, "example.com", 443, ["93.184.216.34"]) is None
    assert eg.decide(eg.Identity("w", [], open=True), "example.com", 22, ["9.9.9.9"]) == ["9.9.9.9"]


def _basic(name: str, token: str) -> str:
    return "Basic " + base64.b64encode(f"{name}:{token}".encode()).decode()


def _config(rules: list[str], *, defaults=("pypi.org:443",), open_=False) -> eg.Config:
    return eg.Config.from_json({"defaults": list(defaults), "workbenches": {
        "workbench-2": {"token_sha256": hashlib.sha256(b"s3cret").hexdigest(), "rules": rules, "open": open_}}})


def test_identify():
    c = _config(["gitgard.home.arpa:3000"])
    anon = c.identify(None)
    assert anon.name == "(no sign-in)" and [r.text for r in anon.rules] == ["pypi.org:443"]
    me = c.identify(_basic("workbench-2", "s3cret"))
    assert me.name == "workbench-2" and [r.text for r in me.rules] == ["pypi.org:443", "gitgard.home.arpa:3000"]
    assert c.identify(_basic("workbench-2", "wrong")) is None
    assert c.identify(_basic("workbench-9", "s3cret")) is None
    assert c.identify("Bearer x") is None
    # A bad rule is skipped, not fatal.
    assert [r.text for r in _config(["ok.example:443", "bad host!"]).identify(_basic("workbench-2", "s3cret")).rules][1:] \
        == ["ok.example:443"]


class FakeSource(eg.ConfigSource):
    def __init__(self, config):
        self.config, self.r, self.logged = config, None, []

    async def blocked(self, who, host, port, reason):
        self.logged.append((who, host, port, reason))


async def _serve(handler):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _http(reader, writer):
    await reader.readuntil(b"\r\n\r\n")
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello")
    await writer.drain()
    writer.close()


async def _echo(reader, writer):
    while data := await reader.read(1024):
        writer.write(data.upper())
        await writer.drain()
    writer.close()


async def _ask(port: int, raw: bytes) -> bytes:
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(raw)
    await w.drain()
    out = await asyncio.wait_for(r.read(65536), 5)
    w.close()
    return out


async def test_proxy_end_to_end():
    web, web_port = await _serve(_http)
    echo, echo_port = await _serve(_echo)
    src = FakeSource(_config([f"127.0.0.1:{web_port}", f"127.0.0.1:{echo_port}"]))
    proxy, port = await _serve(eg.Proxy(src).handle)
    auth = _basic("workbench-2", "s3cret")
    try:
        # Plain HTTP, signed in: allowed by its own rule.
        out = await _ask(port, f"GET http://127.0.0.1:{web_port}/x HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                               f"Proxy-Authorization: {auth}\r\n\r\n".encode())
        assert out.startswith(b"HTTP/1.1 200") and out.endswith(b"hello")
        # Not signed in: only the defaults, so refused and logged.
        out = await _ask(port, f"GET http://127.0.0.1:{web_port}/x HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
        assert out.startswith(b"HTTP/1.1 403") and b"isn't allowed for (no sign-in)" in out
        assert src.logged == [("(no sign-in)", "127.0.0.1", web_port, "not allowed")]
        # Wrong token.
        out = await _ask(port, f"CONNECT 127.0.0.1:{echo_port} HTTP/1.1\r\n"
                               f"Proxy-Authorization: {_basic('workbench-2', 'nope')}\r\n\r\n".encode())
        assert out.startswith(b"HTTP/1.1 407")
        # A CONNECT tunnel carries bytes both ways.
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(f"CONNECT 127.0.0.1:{echo_port} HTTP/1.1\r\nProxy-Authorization: {auth}\r\n\r\n".encode())
        assert (await r.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
        w.write(b"ping")
        assert await asyncio.wait_for(r.readexactly(4), 5) == b"PING"
        w.close()
        # Rules change without a restart.
        src.config = _config([])
        out = await _ask(port, f"CONNECT 127.0.0.1:{echo_port} HTTP/1.1\r\nProxy-Authorization: {auth}\r\n\r\n".encode())
        assert out.startswith(b"HTTP/1.1 403")
        src.config = _config([], open_=True)
        out = await _ask(port, f"GET http://127.0.0.1:{web_port}/ HTTP/1.1\r\nProxy-Authorization: {auth}\r\n\r\n".encode())
        assert out.endswith(b"hello")
    finally:
        for s in (proxy, web, echo):
            s.close()
