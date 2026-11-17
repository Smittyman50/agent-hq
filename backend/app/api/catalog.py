"""MCP server catalog: search an MCP registry (the official one by default) and turn an
entry into a tool server draft for the Tools page.

The backend talks to the registry, never the browser. Entries are normalized to what the
UI needs. A server reachable at a URL (a "remote") can be added straight away. One that
ships only as a package (npm, PyPI, Docker image) needs the package runner, which runs it
in its own container; until then the catalog lists it but can't add it.

Nothing is added without you: the draft opens in the usual form, with every tool set to
Ask me, not given to any agent. A registry listing is metadata, not a review.
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from ..config import settings
from ..db import get_session
from ..models import ToolServer

router = APIRouter(prefix="/api/catalog", tags=["tools"])

OFFICIAL = "io.modelcontextprotocol.registry/official"
# Remote types Agent HQ can connect to (pydantic-ai / FastMCP pick the transport from the URL).
REMOTE_TYPES = {"streamable-http", "sse"}
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
        packages.append({"registry": p.get("registryType") or "", "identifier": p.get("identifier") or "",
                         "version": p.get("version") or "",
                         "transport": (p.get("transport") or {}).get("type") or "stdio",
                         "env": _inputs(p.get("environmentVariables"))})
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
        "needs_runner": not any(r["supported"] for r in remotes) and bool(packages),
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
