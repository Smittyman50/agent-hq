"""The MCP server catalog (app/api/catalog.py) against a fake registry, and tool server
headers: encrypted at rest, sent to the server, kept when the form sends them back."""

from __future__ import annotations

import json

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
        _entry("io.github.someone/filesystem", packages=[
            {"registryType": "npm", "identifier": "@someone/fs-server", "version": "2.0.1",
             "transport": {"type": "stdio"},
             "packageArguments": [{"type": "positional", "valueHint": "allowed_dir", "isRequired": True,
                                   "description": "Directory it may read"},
                                  {"type": "named", "name": "--read-only"},
                                  {"type": "named", "name": "--log-level", "default": "info"}],
             "environmentVariables": [{"name": "FS_TOKEN", "isRequired": True, "isSecret": True},
                                      {"name": "FS_COLOR", "default": "never"}]},
            {"registryType": "pypi", "identifier": "fs-server", "version": "2.0.1", "transport": {"type": "stdio"}}]),
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
    assert names == [("io.github.someone/weather", "1.2.0"), ("com.example/gitea-mcp", "1.2.0"),
                     ("io.github.someone/filesystem", "1.2.0")]
    weather, gitea, fs = r["servers"]
    assert weather["can_add"] and not weather["can_install"]
    assert weather["publisher"] == {"namespace": "io.github.someone", "kind": "github", "who": "someone"}
    assert weather["remotes"][0]["headers"][0]["secret"] is True
    assert not gitea["can_add"] and not gitea["can_install"]
    assert "Docker-image" in gitea["packages"][0]["note"]
    npm, pypi = fs["packages"]
    assert fs["can_install"] and npm["supported"] and pypi["supported"]
    # Required values become placeholders; values it gives are filled in; optional flags are left out.
    assert npm["args"] == ["<allowed_dir>", "--log-level", "info"]
    assert [i["name"] for i in npm["arg_inputs"]] == ["allowed_dir"]
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


# --------------------------------------------------------------------------- package servers

from .test_resources import FakeDocker  # noqa: E402


@pytest.fixture
def docker(monkeypatch):
    from app import resources

    fake = FakeDocker(images=("agent-hq/mcp-runner:latest",))
    monkeypatch.setattr(resources, "docker",
                        lambda: resources.Docker(url="http://docker", transport=httpx.MockTransport(fake.handler)))
    return fake


async def _fs(hq) -> dict:
    return next(x for x in (await hq.http.get("/api/catalog", params={"q": "fs"})).json()["servers"]
                if x["name"].endswith("/filesystem"))


async def test_install_runs_the_package_locked_down_behind_its_token(hq, registry, docker, monkeypatch):
    import hashlib

    from app import db
    from app import webhooks as wh
    from app.config import settings
    from app.models import ToolServer

    monkeypatch.setattr(settings, "secret_key", "a long passphrase for tests")
    fs = await _fs(hq)
    body = {"entry": fs, "package": 0, "name": "files", "env": {"FS_TOKEN": "t0p", "FS_COLOR": "never"},
            "args": ["/data", "--log-level", "info"], "egress_rules": ["gitgard.home.arpa:3000"]}
    r = await hq.http.post("/api/catalog/install", json=body)
    assert r.status_code == 201, r.text
    res, srv = r.json()["resource"], r.json()["server"]
    assert res["kind"] == "package" and res["scope"] == "shared" and res["status"] == "running"
    assert res["package"]["command"] == ["npx", "-y", "@someone/fs-server@2.0.1", "/data", "--log-level", "info"]
    assert "token" not in res["package"] and res["package"]["env"]["FS_TOKEN"].startswith(wh.SEALED)
    assert res["package"]["env"]["FS_COLOR"] == "never"  # the catalog says it isn't secret
    assert res["egress_rules"] == ["registry.npmjs.org:443", "gitgard.home.arpa:3000"]
    assert srv["url"] == "http://files:8790/mcp" and srv["approval_rules"] == {"*": "ask"}
    assert srv["catalog_name"] == "io.github.someone/filesystem" and srv["resource_id"] == res["id"]

    c = docker.containers["ahq-files"]["body"]
    assert c["Image"] == "agent-hq/mcp-runner:latest" and c["Cmd"] == res["package"]["command"]
    env = dict(e.split("=", 1) for e in c["Env"])
    assert env["MCP_RUNNER_NAME"] == "files" and env["FS_TOKEN"] == "t0p"
    assert env["HTTPS_PROXY"].startswith("http://files:") and env["HTTPS_PROXY"].endswith("@egress:8888")
    hc = c["HostConfig"]
    assert hc["ReadonlyRootfs"] and hc["CapDrop"] == ["ALL"] and hc["NetworkMode"] == "agent-hq_workbench"
    assert hc["Mounts"][0]["Target"] == "/work"
    # The bearer token the container checks is the one the tool server sends.
    with db.session() as s:
        t = s.get(ToolServer, srv["id"])
        assert wh.unseal(t.headers["Authorization"]) == f"Bearer {env['MCP_RUNNER_TOKEN']}"
    # It signs in to the egress proxy with its own rules.
    cfg = json.loads(await hq.broker.r.get("ahq:egress:config"))
    token = env["HTTPS_PROXY"].split(":")[2].split("@")[0]
    assert cfg["workbenches"]["files"] == {"token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                                           "rules": res["egress_rules"], "open": False}
    # No agent has it until you give it to one.
    assert res["agents"] == []

    # Changing its environment recreates the container; an encrypted value sent back is kept.
    env_back = {**res["package"]["env"], "FS_COLOR": "always"}
    u = await hq.http.put(f"/api/resources/{res['id']}", json={"package_env": env_back})
    assert u.status_code == 200, u.text
    env2 = dict(e.split("=", 1) for e in docker.containers["ahq-files"]["body"]["Env"])
    assert env2["FS_TOKEN"] == "t0p" and env2["FS_COLOR"] == "always"
    assert env2["MCP_RUNNER_TOKEN"] == env["MCP_RUNNER_TOKEN"]
    a = await hq.http.post(f"/api/resources/{res['id']}/allow", json={"rule": "api.example.com:443"})
    assert a.json()["egress_rules"][-1] == "api.example.com:443"

    # The catalog shows it as added; a PyPI one runs with uvx.
    fs = await _fs(hq)
    assert fs["added_as"] == "files"
    r = await hq.http.post("/api/catalog/install", json={**body, "package": 1, "name": "files-py", "args": []})
    assert r.json()["resource"]["package"]["command"] == ["uvx", "fs-server==2.0.1"]
    assert r.json()["resource"]["egress_rules"] == ["gitgard.home.arpa:3000"]

    await hq.http.delete(f"/api/resources/{res['id']}")
    assert "ahq-files" not in docker.containers
    assert (await hq.http.get(f"/api/tool-servers")).json()[0]["name"] == "files-py"


async def test_install_checks_what_you_fill_in(hq, registry, docker):
    fs = await _fs(hq)
    base = {"entry": fs, "package": 0, "name": "files", "env": {"FS_TOKEN": "x"}, "args": ["/data"]}
    cases = [({"args": ["<allowed_dir>"]}, 422, "allowed_dir"),
             ({"env": {}}, 422, "FS_TOKEN"),
             ({"name": "Bad Name!"}, 422, "name"),
             ({"entry": (await hq.http.get("/api/catalog")).json()["servers"][1]}, 422, "Docker-image")]
    for change, code, words in cases:
        r = await hq.http.post("/api/catalog/install", json={**base, **change})
        assert r.status_code == code and words in r.json()["detail"], (change, r.text)
    assert (await hq.http.post("/api/catalog/install", json=base)).status_code == 201
    r = await hq.http.post("/api/catalog/install", json=base)
    assert r.status_code == 409


async def test_install_without_the_runner_image_says_how_to_build_it(hq, registry, docker):
    docker.images.clear()
    fs = await _fs(hq)
    r = await hq.http.post("/api/catalog/install", json={"entry": fs, "package": 0, "name": "files",
                                                          "env": {"FS_TOKEN": "x"}, "args": ["/d"]})
    res = r.json()["resource"]
    assert res["status"] == "error" and "--profile images build" in res["error"]
