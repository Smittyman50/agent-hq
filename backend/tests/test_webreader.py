"""The web-reader tool server (webreader/webreader_mcp.py): feeds, readable pages, search,
robots.txt, paging of long pages, the private-address guard, and an Agent HQ agent using it.

Runs the real server as a subprocess against a small local website (and a fake SearXNG).
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart

from .conftest import SCRIPTS, _tool_returns

pytest.importorskip("fastmcp")
pytest.importorskip("trafilatura")
pytest.importorskip("feedparser")
from fastmcp import Client  # noqa: E402

SERVER = Path(__file__).resolve().parents[2] / "webreader" / "webreader_mcp.py"

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Proxmox VE releases</title>
<item><title>Proxmox VE 9.1 released</title><link>https://example.test/pve-9-1</link>
<pubDate>Tue, 04 Nov 2026 10:00:00 GMT</pubDate><description>&lt;p&gt;New &lt;b&gt;kernel&lt;/b&gt; and
ZFS 2.4.&lt;/p&gt;</description></item>
<item><title>Proxmox VE 9.0.12</title><link>https://example.test/pve-9-0-12</link>
<pubDate>Mon, 20 Oct 2026 10:00:00 GMT</pubDate><description>Bug fixes.</description></item>
</channel></rss>"""

PARA = ("Proxmox VE 9.1 ships a newer kernel, ZFS 2.4 and improvements to the cluster "
        "resource scheduler. Upgrading from 9.0 is done with apt full-upgrade on each node. ")
ARTICLE = f"""<html><head><title>Proxmox VE 9.1 released</title></head><body>
<nav><a href="/">Home</a> | <a href="/shop">Shop</a> | Subscribe to our newsletter!</nav>
<script>window.tracking = true;</script>
<article><h1>Proxmox VE 9.1 released</h1><p>{PARA}</p><p>{PARA}</p>
<p>Read the <a href="https://example.test/notes">release notes</a> for details.</p></article>
<footer>Copyright 2026. Cookie settings. Advertise with us.</footer></body></html>"""

BLOG = """<html><head><title>Blog</title>
<link rel="alternate" type="application/rss+xml" href="/feed.xml"></head><body>Posts</body></html>"""

LONG = "<html><body><article>" + "".join(
    f"<p>Paragraph {i}: {PARA}</p>" for i in range(60)) + "</article></body></html>"

SEARCH = {"query": "proxmox 9.1", "results": [
    {"title": "Proxmox VE 9.1 released", "url": "https://example.test/pve-9-1",
     "content": "New <b>kernel</b> and ZFS 2.4", "engines": ["duckduckgo", "brave"],
     "publishedDate": "2026-11-04T10:00:00"},
    {"title": "Upgrade guide", "url": "https://example.test/upgrade", "content": "How to upgrade",
     "engines": ["bing"]}], "suggestions": ["proxmox 9.1 upgrade"]}


class Site(BaseHTTPRequestHandler):
    seen: list[str] = []

    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):  # noqa: N802
        Site.seen.append(self.path)
        path = self.path.split("?")[0]
        routes = {
            "/feed.xml": ("application/rss+xml", RSS),
            "/blog": ("text/html", BLOG),
            "/article": ("text/html", ARTICLE),
            "/private/secret": ("text/html", ARTICLE),
            "/long": ("text/html", LONG),
            "/robots.txt": ("text/plain", "User-agent: *\nDisallow: /private/\n"),
            "/search": ("application/json", json.dumps(SEARCH)),
            "/old": None,
        }
        if path == "/old":
            self.send_response(301)
            self.send_header("Location", "/article")
            self.end_headers()
            return
        if path not in routes:
            self.send_response(404)
            self.end_headers()
            return
        ctype, body = routes[path]
        if path == "/search":
            assert "format=json" in self.path
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def site():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _start(env: dict) -> tuple[subprocess.Popen, str]:
    port = _free_port()
    proc = subprocess.Popen([sys.executable, str(SERVER)],
                            env={**os.environ, "WEBREADER_PORT": str(port), **env},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 30
    while time.time() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return proc, f"http://127.0.0.1:{port}/mcp"
        time.sleep(0.2)
    proc.kill()
    raise RuntimeError("web reader did not start")


@pytest.fixture(scope="module")
def reader(site):
    proc, url = _start({"WEBREADER_ALLOW_PRIVATE": "true", "SEARXNG_URL": site, "WEBREADER_MAX_CHARS": "2000"})
    yield url
    proc.terminate()
    proc.wait(10)


async def _call(server: str, tool: str, **args):
    async with Client(server) as c:
        res = await c.call_tool(tool, args)
        return res.structured_content if res.structured_content is not None else json.loads(res.content[0].text)


async def test_read_feed_and_feed_discovery(reader, site):
    out = await _call(reader, "read_feed", url=f"{site}/feed.xml", limit=1)
    assert out["feed"] == "Proxmox VE releases"
    assert out["items"] == [{"title": "Proxmox VE 9.1 released", "link": "https://example.test/pve-9-1",
                             "published": "Tue, 04 Nov 2026 10:00:00 GMT", "summary": "New kernel and ZFS 2.4."}]
    # A site's page that links to its feed works too.
    out = await _call(reader, "read_feed", url=f"{site}/blog")
    assert out["url"] == f"{site}/feed.xml" and len(out["items"]) == 2
    out = await _call(reader, "read_feed", url=f"{site}/article")
    assert "without a linked feed" in out["error"]


async def test_fetch_page_returns_the_article_not_the_clutter(reader, site):
    out = await _call(reader, "fetch_page", url=f"{site}/old")  # follows the redirect
    assert out["url"] == f"{site}/article" and out["title"] == "Proxmox VE 9.1 released"
    assert "ZFS 2.4" in out["text"] and "apt full-upgrade" in out["text"]
    assert "[release notes](https://example.test/notes)" in out["text"]
    for junk in ("newsletter", "tracking", "Cookie settings", "Advertise"):
        assert junk not in out["text"]
    assert out["more"] is False


async def test_long_pages_come_back_in_parts(reader, site):
    first = await _call(reader, "fetch_page", url=f"{site}/long")
    assert len(first["text"]) == 2000 and first["more"] and first["next_offset"] == 2000
    second = await _call(reader, "fetch_page", url=f"{site}/long", offset=first["next_offset"])
    assert second["text"] and second["text"] != first["text"]
    assert first["total_chars"] > 4000


async def test_robots_txt_is_respected(reader, site):
    out = await _call(reader, "fetch_page", url=f"{site}/private/secret")
    assert "robots.txt" in out["error"]


async def test_search_through_searxng(reader):
    out = await _call(reader, "search_web", query="proxmox 9.1", limit=1, time_range="week")
    assert out["results"] == [{"title": "Proxmox VE 9.1 released", "url": "https://example.test/pve-9-1",
                               "snippet": "New kernel and ZFS 2.4", "published": "2026-11-04T10:00:00",
                               "engines": ["duckduckgo", "brave"]}]
    assert any("time_range=week" in p for p in Site.seen)


async def test_private_addresses_are_refused_by_default_and_search_needs_searxng(site):
    proc, url = _start({})
    try:
        async with Client(url) as c:
            names = {t.name for t in await c.list_tools()}
        assert names == {"read_feed", "fetch_page"}  # no SEARXNG_URL, no search tool
        out = await _call(url, "fetch_page", url=f"{site}/article")
        assert "private/LAN address" in out["error"] and "WEBREADER_ALLOW_HOSTS" in out["error"]
    finally:
        proc.terminate()
        proc.wait(10)
    # ...unless that host is allowed explicitly.
    proc, url = _start({"WEBREADER_ALLOW_HOSTS": "127.0.0.1"})
    try:
        out = await _call(url, "fetch_page", url=f"{site}/article")
        assert "ZFS 2.4" in out["text"]
    finally:
        proc.terminate()
        proc.wait(10)


def researcher(messages, info):
    returns = _tool_returns(messages)
    if not returns:
        feed = [m for m in messages][-1].parts[-1].content.split("feed at ")[1].strip()
        return ModelResponse(parts=[ToolCallPart("read_feed", {"url": feed, "limit": 1})])
    item = returns[-1].content["items"][0]
    return ModelResponse(parts=[TextPart(f"Latest: {item['title']} ({item['link']})")])


async def test_an_agent_reads_a_feed_through_agent_hq(hq, reader, site):
    SCRIPTS["researcher"] = researcher
    srv = (await hq.http.post("/api/tool-servers", json={"name": "web", "transport": "http", "url": reader,
                                                   "prefix_tools": False})).json()
    await hq.start_worker()
    a = await hq.agent(name="Scarlett", model="fake:researcher", tool_server_ids=[srv["id"]], memory_enabled=False)
    t = await hq.thread(a["id"])
    await hq.say(t["id"], f"What's new? Check the feed at {site}/feed.xml")
    assert await hq.wait_agent_replies(t["id"], 1, timeout=30) == [
        "Latest: Proxmox VE 9.1 released (https://example.test/pve-9-1)"]
