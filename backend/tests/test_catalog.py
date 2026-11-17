"""The MCP server catalog (app/api/catalog.py) against a fake registry, and tool server
headers: encrypted at rest, sent to the server, kept when the form sends them back."""

from __future__ import annotations

import httpx
import pytest

from app.api import catalog

OFFICIAL = catalog.OFFICIAL


def _entry(name, *, remotes=None, packages=None, status="active", latest=True, version="1.2.0"):
    return {"server": {"name": name, "description": f"{name} tools", "version": version,
                       "repository": {"url": f"https://github.com/x/{name.split('/')[-1]}", "source": "github"},
                       "remotes": remotes or [], "packages": packages or []},
            "_meta": {OFFICIAL: {"status": status, "isLatest": latest,
                                 "publishedAt": "2026-10-01T00:00:00Z"}}}


REGISTRY = {
    "servers": [
        _entry("io.github.someone/weather", remotes=[
            {"type": "streamable-http", "url": "https://weather.example/mcp",
             "headers": [{"name": "Authorization", "description": "Bearer token",
                          "isRequired": True, "isSecret": True}]}]),
        _entry("com.example/gitea-mcp", packages=[
            {"registryType": "oci", "identifier": "docker.io/example/gitea-mcp", "version": "0.4.1",
             "transport": {"type": "stdio"},
             "environmentVariables": [{"name": "GITEA_TOKEN", "isRequired": True, "isSecret": True}]}]),
        _entry("io.github.someone/weather", version="1.0.0", latest=False,
               remotes=[{"type": "streamable-http", "url": "https://old.example/mcp"}]),
        _entry("io.github.someone/gone", status="deleted",
               remotes=[{"type": "streamable-http", "url": "https://gone.example/mcp"}]),
    ],
    "metadata": {"nextCursor": "abc", "count": 4},
}


@pytest.fixture
def registry(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        assert req.url.path == "/v0/servers"
        return httpx.Response(200, json=REGISTRY)

    monkeypatch.setattr(catalog, "_transport", httpx.MockTransport(handler))
    catalog._cache.clear()
    yield seen
    catalog._cache.clear()


async def test_search_lists_latest_active_servers(hq, registry):
    r = (await hq.http.get("/api/catalog", params={"q": "weather"})).json()
    assert registry[0].url.params["search"] == "weather"
    assert r["next_cursor"] == "abc"
    names = [(x["name"], x["version"]) for x in r["servers"]]
    assert names == [("io.github.someone/weather", "1.2.0"), ("com.example/gitea-mcp", "1.2.0")]
    weather, gitea = r["servers"]
    assert weather["can_add"] and not weather["needs_runner"]
    assert weather["publisher"] == {"namespace": "io.github.someone", "kind": "github", "who": "someone"}
    assert weather["remotes"][0]["headers"][0]["secret"] is True
    assert not gitea["can_add"] and gitea["needs_runner"]
    assert gitea["publisher"]["who"] == "example.com"
    assert gitea["packages"][0]["env"][0]["name"] == "GITEA_TOKEN"


async def test_draft_asks_for_everything_and_isnt_given_to_agents(hq, registry):
    weather = (await hq.http.get("/api/catalog")).json()["servers"][0]
    d = (await hq.http.post("/api/catalog/draft", json=weather)).json()
    assert d["name"] == "weather" and d["url"] == "https://weather.example/mcp"
    assert d["approval_rules"] == {"*": "ask"} and d["all_agents"] is False
    assert d["headers"] == {"Authorization": ""}
    assert d["catalog_name"] == "io.github.someone/weather"

    d["headers"] = {"Authorization": "Bearer t0ken"}
    made = (await hq.http.post("/api/tool-servers", json=d))
    assert made.status_code == 201, made.text
    # Listed as added, and the next draft gets a name that's free.
    again = (await hq.http.get("/api/catalog", params={"q": "x"})).json()["servers"][0]
    assert again["added_as"] == "weather" and again["suggested_name"] == "weather-2"

    gitea = (await hq.http.get("/api/catalog")).json()["servers"][1]
    bad = await hq.http.post("/api/catalog/draft", json=gitea)
    assert bad.status_code == 422


async def test_registry_down_is_a_clear_error(hq, monkeypatch):
    monkeypatch.setattr(catalog, "_transport",
                        httpx.MockTransport(lambda req: httpx.Response(503)))
    catalog._cache.clear()
    r = await hq.http.get("/api/catalog", params={"q": "down"})
    assert r.status_code == 502 and "503" in r.json()["detail"]


async def test_headers_are_encrypted_and_sent(hq, monkeypatch):
    from app import db
    from app import webhooks as wh
    from app.config import settings
    from app.models import ToolServer
    from app.runtime import mcp_toolset

    plain = (await hq.http.post("/api/tool-servers", json={
        "name": "before-key", "url": "http://a.example/mcp", "headers": {"X-Key": "one"}})).json()
    monkeypatch.setattr(settings, "secret_key", "a long passphrase for tests")
    assert wh.seal_existing() == 1
    srv = (await hq.http.post("/api/tool-servers", json={
        "name": "remote", "url": "http://b.example/mcp", "headers": {"Authorization": "Bearer abc"}})).json()
    stored = srv["headers"]["Authorization"]
    assert stored.startswith(wh.SEALED)
    # The form sends the sealed value back untouched: it stays the same secret.
    again = (await hq.http.put(f"/api/tool-servers/{srv['id']}", json=srv)).json()
    assert again["headers"]["Authorization"] == stored
    with db.session() as s:
        t = s.get(ToolServer, srv["id"])
        assert mcp_toolset(t)  # builds with the decrypted header
        assert wh.unseal(t.headers["Authorization"]) == "Bearer abc"
        assert wh.unseal(s.get(ToolServer, plain["id"]).headers["X-Key"]) == "one"
