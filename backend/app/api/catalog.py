"""MCP server catalog: search an MCP registry (the official one by default) and turn an
entry into a tool server draft for the Tools page.

The backend talks to the registry, never the browser. Entries are normalized to what the
UI needs. A server reachable at a URL (a "remote") is added as a plain HTTP tool server.
One published as an npm or PyPI package is installed as a package server: a container of
its own from the runner image (mcp-runner/), see app/resources.py. Docker-image packages
aren't supported yet.

Nothing is trusted by default: whatever you add or install starts with every tool set to
Ask me and isn't given to any agent. A registry listing is metadata, not a review.
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlmodel import Session, select

from .. import resources as rs
from ..config import settings
from ..db import get_session
from ..models import Resource, ToolServer
from ..webhooks import seal
from .resources import _view, plain_names, seal_env

router = APIRouter(prefix="/api/catalog", tags=["tools"])

OFFICIAL = "io.modelcontextprotocol.registry/official"
# Remote types Agent HQ can connect to (pydantic-ai / FastMCP pick the transport from the URL).
REMOTE_TYPES = {"streamable-http", "sse"}
# Package registries the runner can install from, and how it says they run.
PACKAGE_REGISTRIES = {"npm": "npx", "pypi": "uvx"}
NAME_OK = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
PLACEHOLDER = re.compile(r"^<[^>]+>$")
_CACHE_SECONDS = 300
_cache: dict[tuple, tuple[float, dict]] = {}
# Tests swap in a transport that fakes the registry.
_transport: httpx.AsyncBaseTransport | None = None


def _clean_name(server_name: str) -> str:
    """io.github.someone/gitea-mcp -> gitea-mcp: short, and fine as a tool-name prefix."""
    tail = server_name.rsplit("/", 1)[-1]
    slug = re.sub(r"[^a-z0-9]+", "-", tail.lower()).strip("-")
    return (slug or "mcp")[:40]


def _unique(name: str, taken: set[str]) -> str:
    if name not in taken:
        return name
    n = 2
    while f"{name}-{n}" in taken:
        n += 1
    return f"{name}-{n}"


def _inputs(items: list[dict] | None) -> list[dict]:
    """Headers or environment variables the entry says it takes."""
    out = []
    for x in items or []:
        if not isinstance(x, dict) or not x.get("name"):
            continue
        out.append({"name": x["name"], "description": x.get("description") or "",
                    "required": bool(x.get("isRequired")), "secret": bool(x.get("isSecret")),
                    "value": x.get("value") or x.get("default") or ""})
    return out


def _arguments(items: list[dict] | None) -> tuple[list[str], list[dict]]:
    """A package's arguments as a command line: values it gives are filled in, others
    become <placeholders> to replace. Also returns them as inputs to show."""
    args, inputs = [], []
    for a in items or []:
        if not isinstance(a, dict):
            continue
        value = a.get("value") or a.get("default") or ""
        hint = a.get("valueHint") or a.get("name") or "value"
        named = a.get("type") == "named"
        if not value and not a.get("isRequired"):
            if named and not a.get("valueHint"):
                continue  # an optional flag: leave it to you
            inputs.append({"name": a.get("name") or hint, "description": a.get("description") or "",
                           "required": False, "secret": bool(a.get("isSecret")), "value": ""})
            continue
        token = value or f"<{hint}>"
        if named:
            args += [a.get("name") or "", token] if (value or a.get("valueHint")) else [a.get("name") or ""]
        else:
            args.append(token)
        if not value:
            inputs.append({"name": a.get("name") or hint, "description": a.get("description") or "",
                           "required": True, "secret": bool(a.get("isSecret")), "value": ""})
    return [x for x in args if x], inputs


def _publisher(server_name: str) -> dict:
    """Who published it, as far as the name shows. io.github.<user>/... was proved by a
    GitHub sign-in; a reversed domain (com.example/...) by DNS or HTTP on that domain."""
    ns = server_name.split("/", 1)[0]
    if ns.startswith("io.github."):
        return {"namespace": ns, "kind": "github", "who": ns[len("io.github."):]}
    parts = ns.split(".")
    return {"namespace": ns, "kind": "domain", "who": ".".join(reversed(parts))}


def normalize(entry: dict, taken: set[str], added: dict[str, str]) -> dict:
    srv = entry.get("server") or entry
    meta = (entry.get("_meta") or {}).get(OFFICIAL) or {}
    name = srv.get("name", "")
    remotes = []
    for r in srv.get("remotes") or []:
        url = r.get("url") or ""
        kind = r.get("type") or ""
        supported = kind in REMOTE_TYPES and bool(url)
        templated = bool(re.search(r"\{[^}]+\}", url))
        remotes.append({"type": kind, "url": url, "headers": _inputs(r.get("headers")),
                        "variables": sorted(re.findall(r"\{([^}]+)\}", url)),
                        "supported": supported,
                        "note": "" if supported else f"Agent HQ can't connect to a '{kind}' remote",
                        "templated": templated})
    packages = []
    for p in srv.get("packages") or []:
        registry = p.get("registryType") or ""
        transport = (p.get("transport") or {}).get("type") or "stdio"
        args, arg_inputs = _arguments(p.get("packageArguments"))
        note = ""
        if registry not in PACKAGE_REGISTRIES:
            note = ("Docker-image servers can't be installed yet" if registry == "oci"
                    else f"Agent HQ can't install {registry or 'unknown'} packages")
        elif transport != "stdio":
            note = f"this package serves {transport} itself; only stdio packages can be installed"
        packages.append({"registry": registry, "identifier": p.get("identifier") or "",
                         "version": p.get("version") or "", "transport": transport,
                         "env": _inputs(p.get("environmentVariables")),
                         "args": args, "arg_inputs": arg_inputs,
                         "supported": not note and bool(p.get("identifier")), "note": note})
    repo = srv.get("repository") or {}
    return {
        "name": name,
        "title": srv.get("title") or "",
        "description": srv.get("description") or "",
        "version": srv.get("version") or "",
        "website": srv.get("websiteUrl") or "",
        "repository": repo.get("url") or "",
        "publisher": _publisher(name),
        "status": meta.get("status") or "active",
        "published_at": meta.get("publishedAt"),
        "latest": meta.get("isLatest", True),
        "updated_at": meta.get("updatedAt"),
        "remotes": remotes,
        "packages": packages,
        "can_add": any(r["supported"] for r in remotes),
        "can_install": any(p["supported"] for p in packages),
        "added_as": added.get(name),  # the tool server already made from it, if any
        "suggested_name": _unique(_clean_name(name), taken),
    }


def draft(entry: dict, remote_index: int) -> dict:
    """A ToolServerIn body for one of the entry's remotes, for the UI to open in the form."""
    try:
        r = entry["remotes"][remote_index]
    except (IndexError, KeyError, TypeError):
        raise HTTPException(422, "that entry has no such remote") from None
    if not r["supported"]:
        raise HTTPException(422, r["note"])
    desc = entry["title"] or entry["description"]
    if entry["title"] and entry["description"]:
        desc = f"{entry['title']}: {entry['description']}"
    return {
        "name": entry["suggested_name"], "description": desc[:300],
        "transport": "http", "url": r["url"], "command": None, "args": [], "env": {},
        "headers": {h["name"]: h["value"] for h in r["headers"]},
        "catalog_name": entry["name"], "catalog_version": entry["version"],
        "prefix_tools": True, "desktop": False, "all_agents": False, "read_only": False,
        # Every tool asks until you've looked at them (Test connection, then relax rules).
        "approval_rules": {"*": "ask"}, "enabled": True,
    }


async def _get(path: str, params: dict) -> dict:
    if not settings.mcp_registry_url:
        raise HTTPException(404, "the MCP server catalog is off (AHQ_MCP_REGISTRY_URL is empty)")
    key = (settings.mcp_registry_url, path, tuple(sorted(params.items())))
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < _CACHE_SECONDS:
        return hit[1]
    kwargs: dict[str, Any] = {"timeout": 15, "headers": {"Accept": "application/json",
                                                         "User-Agent": "agent-hq"}}
    if _transport is not None:
        kwargs["transport"] = _transport
    try:
        async with httpx.AsyncClient(**kwargs) as c:
            resp = await c.get(settings.mcp_registry_url + path, params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"the registry answered {e.response.status_code}") from e
    except (httpx.HTTPError, ValueError) as e:
        raise HTTPException(502, f"could not reach the registry at {settings.mcp_registry_url}: {e}") from e
    _cache[key] = (time.monotonic(), data)
    return data


def _known(s: Session) -> tuple[set[str], dict[str, str]]:
    rows = s.exec(select(ToolServer)).all()
    return {t.name for t in rows}, {t.catalog_name: t.name for t in rows if t.catalog_name}


@router.get("")
async def search(q: str = "", cursor: str | None = None, limit: int = Query(30, ge=1, le=100),
                 include_inactive: bool = False, s: Session = Depends(get_session)):
    """One page of the registry, newest version of each server only."""
    params: dict[str, Any] = {"limit": limit}
    if q.strip():
        params["search"] = q.strip()
    if cursor:
        params["cursor"] = cursor
    data = await _get("/v0/servers", params)
    taken, added = _known(s)
    entries = [e for e in data.get("servers") or []
               if ((e.get("_meta") or {}).get(OFFICIAL) or {}).get("isLatest", True)]
    items = [normalize(e, taken, added) for e in entries]
    if not include_inactive:
        items = [x for x in items if x["status"] == "active"]
    meta = data.get("metadata") or {}
    return {"registry": settings.mcp_registry_url, "servers": items,
            "next_cursor": meta.get("nextCursor") or meta.get("next_cursor")}


@router.post("/draft")
def make_draft(body: dict, remote: int = 0):
    """Body: a server as /api/catalog returned it. Returns the tool server to review."""
    if not isinstance(body, dict) or "remotes" not in body:
        raise HTTPException(422, "send a catalog entry")
    return draft(body, remote)


class InstallIn(BaseModel):
    entry: dict           # the server as /api/catalog returned it
    package: int = 0      # which of its packages
    name: str             # the tool server's and container's name
    env: dict[str, str] = {}
    args: list[str] = []
    egress_rules: list[str] = []   # hosts it may reach besides PyPI (and npm, for npm packages)


@router.post("/install", status_code=201)
async def install(body: InstallIn, request: Request, s: Session = Depends(get_session)):
    """Install a package server: a container of its own running the package, and the tool
    server agents reach it at. Every tool starts on Ask me; no agent has it yet."""
    try:
        pkg = body.entry["packages"][body.package]
    except (KeyError, IndexError, TypeError):
        raise HTTPException(422, "that entry has no such package") from None
    if not pkg.get("supported"):
        raise HTTPException(422, pkg.get("note") or "this package can't be installed")
    name = body.name.strip().lower()
    if not NAME_OK.match(name):
        raise HTTPException(422, "the name must start with a letter and use only a-z, 0-9 and -, "
                                 "40 characters at most")
    taken = {r.name for r in s.exec(select(Resource)).all()} | {t.name for t in s.exec(select(ToolServer)).all()}
    if name in taken:
        raise HTTPException(409, f"{name} is already taken")
    args = [a.strip() for a in body.args if a.strip()]
    if blanks := [a for a in args if PLACEHOLDER.match(a)]:
        raise HTTPException(422, f"fill in {', '.join(blanks)} in the arguments")
    missing = [e["name"] for e in pkg.get("env") or [] if e.get("required") and not (body.env.get(e["name"]) or "").strip()]
    if missing:
        raise HTTPException(422, f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} required")
    entry = body.entry
    token = secrets.token_urlsafe(32)
    res = Resource(
        name=name, kind=rs.PACKAGE, scope="shared",
        description=(entry.get("title") or entry.get("description") or "")[:200],
        egress_rules=rs.check_rules(rs.PACKAGE_EGRESS.get(pkg["registry"], []) + body.egress_rules),
        package={"registry": pkg["registry"], "identifier": pkg["identifier"], "version": pkg.get("version") or "",
                 "args": args, "env": seal_env({k: v for k, v in body.env.items() if v is not None},
                                               plain_names(pkg.get("env"))),
                 "token": seal(token), "catalog_name": entry.get("name"),
                 "catalog_version": entry.get("version"), "repository": entry.get("repository") or "",
                 "inputs": (pkg.get("env") or []) + (pkg.get("arg_inputs") or [])})
    s.add(res)
    s.flush()
    desc = entry.get("title") or entry.get("description") or ""
    srv = ToolServer(name=name, description=desc[:300], url=f"http://{name}:{rs.PACKAGE_MCP_PORT}/mcp",
                     headers={"Authorization": seal(f"Bearer {token}")}, approval_rules={"*": "ask"},
                     catalog_name=entry.get("name"), catalog_version=entry.get("version"),
                     resource_id=res.id)
    s.add(srv)
    d = rs.docker()
    if d.enabled:
        try:
            await rs.provision(res, d)
        except rs.DockerError as e:
            res.status, res.error = "error", str(e)
    else:
        res.status = "unknown"
        res.error = ("Agent HQ has no Docker access, so it registered the tool server but created no "
                     "container. See docs/desktops-and-workbenches.md.")
    s.add(res)
    s.commit()
    s.refresh(res)
    s.refresh(srv)
    await rs.publish_egress(request.app.state.broker.r, s)
    return {"resource": await _view(s, res, request), "server": srv}
