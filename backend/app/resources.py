"""Resources: desktops, workbenches and package servers you create from the UI and give
to agents.

A package server is an MCP server from the catalog (app/api/catalog.py) published as an
npm or PyPI package. It runs in a container of its own (mcp-runner/), locked down and
networked like a workbench, and serves the package over HTTP with a bearer token of its
own, so nothing else on the workbench network can call it.

A resource is one container (an agent desktop, or a workbench) plus the tool servers it
serves. Agent HQ creates the container through the Docker API (the docker-proxy service
in docker-compose.yml), registers its tool servers, and ticks them on the agents you give
it to:

* **dedicated**: one agent's own. Nobody else can be given it.
* **shared**: any number of agents. A shared workbench is just shared files. A shared
  desktop is used by one conversation at a time: the first desktop call takes a lease,
  other conversations wait for it (AHQ_DESKTOP_LEASE_WAIT), and it is let go when that
  run ends or AHQ_DESKTOP_LEASE_TTL after its last desktop call.

Who has a resource is not stored twice: it is the agents whose tool_server_ids include
its servers. Containers you run yourself (desktop-N / workbench-N services in
docker-compose.yml) can be adopted: they get a Resource row (managed=False) for their
existing tool servers, and compose keeps running the container until you take it over.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import re
import secrets
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException
from sqlmodel import Session, select

from .config import settings
from .models import AgentDef, Resource, ToolServer, _now

log = logging.getLogger("agent_hq.resources")

KINDS = ("desktop", "workbench")  # made on the resources page
PACKAGE = "package"                # made by installing from the MCP server catalog
# Kinds that go out only through the egress proxy, with rules of their own.
EGRESS_KINDS = ("workbench", PACKAGE)
SCOPES = ("dedicated", "shared")
NAME_RE = re.compile(r"^(desktop|workbench)-(\d+)$")
LABEL = "agent-hq.resource"

# The ports each kind serves MCP on (see desktop/ and workbench/).
DESKTOP_MCP_PORT = 8765
BROWSER_MCP_PORT = 8931
WORKBENCH_MCP_PORT = 8770
PACKAGE_MCP_PORT = 8790
# What a package server may reach besides the defaults (PyPI), to download itself.
PACKAGE_EGRESS = {"npm": ["registry.npmjs.org:443"], "pypi": []}


def package_command(pkg: dict[str, Any]) -> list[str]:
    """The stdio command the runner wraps: npx for npm, uvx for PyPI."""
    ident, version, args = pkg["identifier"], pkg.get("version") or "", list(pkg.get("args") or [])
    if pkg["registry"] == "npm":
        return ["npx", "-y", f"{ident}@{version}" if version else ident, *args]
    if pkg["registry"] == "pypi":
        return ["uvx", f"{ident}=={version}" if version else ident, *args]
    raise ValueError(f"can't run a {pkg['registry']} package")


def package_env(pkg: dict[str, Any]) -> list[str]:
    from .webhooks import unseal

    return [f"{k}={unseal(v)}" for k, v in (pkg.get("env") or {}).items()]


def viewer_url(res: Resource) -> str | None:
    """Where the UI shows a desktop: Caddy routes /desktops/desktop-N/ to it."""
    return f"/desktops/{res.name}/" if res.kind == "desktop" else None


# --------------------------------------------------------------------------- Docker


class DockerError(RuntimeError):
    pass


class Docker:
    """Just enough of the Docker Engine API, over HTTP (the docker-proxy service)."""

    def __init__(self, url: str | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.url = (settings.docker_url if url is None else url).rstrip("/")
        self._transport = transport

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    async def _req(self, method: str, path: str, ok: tuple[int, ...] = (), **kw) -> httpx.Response:
        if not self.enabled:
            raise DockerError("Agent HQ has no Docker access (set AHQ_DOCKER_URL; see the README's "
                              "Resources section)")
        try:
            async with httpx.AsyncClient(base_url=self.url, timeout=120,
                                         transport=self._transport) as c:
                r = await c.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise DockerError(f"can't reach Docker at {self.url}: {e}") from e
        if r.status_code >= 400 and r.status_code not in ok:
            try:
                msg = r.json().get("message") or r.text
            except ValueError:
                msg = r.text
            raise DockerError(f"Docker: {msg.strip() or r.status_code}")
        return r

    async def inspect(self, name: str) -> dict | None:
        if not name:
            return None
        r = await self._req("GET", f"/containers/{name}/json", ok=(404,))
        return None if r.status_code == 404 else r.json()

    async def create(self, name: str, body: dict) -> str:
        r = await self._req("POST", "/containers/create", params={"name": name}, json=body)
        return r.json()["Id"]

    async def start(self, name: str) -> None:
        await self._req("POST", f"/containers/{name}/start", ok=(304,))

    async def stop(self, name: str) -> None:
        await self._req("POST", f"/containers/{name}/stop", params={"t": 10}, ok=(304, 404))

    async def restart(self, name: str) -> None:
        await self._req("POST", f"/containers/{name}/restart", params={"t": 10})

    async def remove(self, name: str) -> None:
        await self._req("DELETE", f"/containers/{name}", params={"force": "true"}, ok=(404,))

    async def remove_volume(self, name: str) -> None:
        await self._req("DELETE", f"/volumes/{name}", ok=(404,))

    async def volume(self, name: str) -> dict | None:
        r = await self._req("GET", f"/volumes/{name}", ok=(404,))
        return None if r.status_code == 404 else r.json()

    async def connect(self, network: str, name: str, aliases: list[str]) -> None:
        await self._req("POST", f"/networks/{network}/connect",
                        json={"Container": name, "EndpointConfig": {"Aliases": aliases}})

    async def disconnect(self, network: str, name: str) -> None:
        await self._req("POST", f"/networks/{network}/disconnect",
                        json={"Container": name, "Force": True}, ok=(404,))

    async def image_exists(self, image: str) -> bool:
        r = await self._req("GET", f"/images/{image}/json", ok=(404,))
        return r.status_code != 404

    async def compose_container(self, service: str) -> dict | None:
        """The container compose runs for a service of this project, if any."""
        filters = {"label": [f"com.docker.compose.project={settings.compose_project}",
                             f"com.docker.compose.service={service}"]}
        r = await self._req("GET", "/containers/json",
                            params={"all": "true", "filters": json.dumps(filters)})
        found = r.json()
        return found[0] if found else None

    async def compose_services(self) -> list[str]:
        """Services of this compose project named like resources (desktop-N, workbench-N)."""
        filters = {"label": [f"com.docker.compose.project={settings.compose_project}"]}
        r = await self._req("GET", "/containers/json",
                            params={"all": "true", "filters": json.dumps(filters)})
        names = {(c.get("Labels") or {}).get("com.docker.compose.service", "") for c in r.json()}
        return sorted(n for n in names if NAME_RE.match(n))


def docker() -> Docker:
    return Docker()


# --------------------------------------------------------------------------- containers


def container_name(res: Resource) -> str:
    return f"ahq-{res.name}"


def volume_name(res: Resource) -> str:
    return f"ahq-{res.name}"


def _labels(res: Resource) -> dict[str, str]:
    return {LABEL: res.id, "agent-hq.kind": res.kind, "agent-hq.name": res.name}


def _data_mount(res: Resource) -> dict:
    return {"Type": "volume", "Source": volume_name(res),
            "Target": "/config" if res.kind == "desktop" else "/work",
            "VolumeOptions": {"Labels": _labels(res)}}


def spec(res: Resource, image: str, mounts: list[dict] | None = None) -> dict:
    """The container: a desktop as linuxserver/webtop wants it, a workbench or package
    server locked down."""
    mounts = mounts or [_data_mount(res)]
    restart = {"Name": "unless-stopped"}
    if res.kind == "desktop":
        net = settings.resource_network
        return {
            "Image": image,
            "Env": ["PUID=1000", "PGID=1000", f"TZ={settings.timezone}",
                    f"SUBFOLDER=/desktops/{res.name}/"],
            "Labels": _labels(res),
            "HostConfig": {"ShmSize": 1 << 30, "RestartPolicy": restart, "Mounts": mounts,
                           "NetworkMode": net},
            "NetworkingConfig": {"EndpointsConfig": {net: {"Aliases": [res.name]}}},
        }
    net = settings.workbench_network
    proxy = proxy_url(res)
    body = {
        "Image": image,
        "Env": [f"HTTPS_PROXY={proxy}", f"HTTP_PROXY={proxy}", f"https_proxy={proxy}",
                f"http_proxy={proxy}", "NO_PROXY=localhost,127.0.0.1", "no_proxy=localhost,127.0.0.1"],
        "Labels": _labels(res),
        "HostConfig": {"RestartPolicy": restart, "Mounts": mounts, "NetworkMode": net,
                       "ReadonlyRootfs": True, "Tmpfs": {"/tmp": ""}, "CapDrop": ["ALL"],
                       "SecurityOpt": ["no-new-privileges:true"], "NanoCpus": 2_000_000_000,
                       "Memory": 2 * 1024 ** 3, "PidsLimit": 512},
        "NetworkingConfig": {"EndpointsConfig": {net: {"Aliases": [res.name]}}},
    }
    if res.kind == PACKAGE:
        from .webhooks import unseal

        pkg = res.package or {}
        body["Env"] += [f"MCP_RUNNER_NAME={res.name}",
                        f"MCP_RUNNER_TOKEN={unseal(pkg.get('token') or '')}", *package_env(pkg)]
        body["Cmd"] = package_command(pkg)
        body["HostConfig"].update({"NanoCpus": 1_000_000_000, "Memory": 1024 ** 3, "PidsLimit": 256})
    return body


def proxy_url(res: Resource) -> str:
    """The egress proxy, signed in as this workbench (its own rules apply)."""
    if res.egress_token:
        return f"http://{res.name}:{res.egress_token}@egress:8888"
    return "http://egress:8888"


# Whether each workbench's container signs in to the egress proxy with its current token
# (one made before per-workbench rules, or before a token change, gets the defaults only).
_proxy_current: dict[str, bool] = {}


def proxy_current(res: Resource) -> bool | None:
    return _proxy_current.get(res.id) if res.kind in EGRESS_KINDS else None


def _mounts_from(info: dict) -> list[dict]:
    """An existing container's mounts, to recreate it with the same data."""
    out = []
    for m in info.get("Mounts") or []:
        if m.get("Type") == "bind":
            out.append({"Type": "bind", "Source": m["Source"], "Target": m["Destination"],
                        "ReadOnly": not m.get("RW", True)})
        elif m.get("Type") == "volume" and m.get("Name"):
            out.append({"Type": "volume", "Source": m["Name"], "Target": m["Destination"],
                        "ReadOnly": not m.get("RW", True)})
    return out


def _image_for(res: Resource) -> str:
    if res.kind == PACKAGE:
        return settings.mcp_runner_image
    return settings.desktop_image if res.kind == "desktop" else settings.workbench_image


def _status_of(info: dict | None) -> str:
    if info is None:
        return "missing"
    state = (info.get("State") or {}).get("Status", "")
    return "running" if state in ("running", "restarting") else "stopped"


async def refresh(res: Resource, d: Docker | None = None) -> Resource:
    """Update the resource's status from Docker (unchanged when there is no Docker access)."""
    d = d or docker()
    if not d.enabled or not res.container:
        if res.status in ("new", ""):
            res.status = "unknown"  # no Docker access, or no container found for it
        return res
    try:
        info = await d.inspect(res.container)
        res.status = _status_of(info)
        if info is not None and res.kind in EGRESS_KINDS:
            env = (info.get("Config") or {}).get("Env") or []
            _proxy_current[res.id] = bool(res.egress_token) and f"HTTPS_PROXY={proxy_url(res)}" in env
        if res.status != "error":
            res.error = None
    except DockerError as e:
        res.status, res.error = "error", str(e)
    return res


async def provision(res: Resource, d: Docker | None = None, *, mounts: list[dict] | None = None,
                    image: str | None = None) -> Resource:
    """Create and start the resource's container."""
    d = d or docker()
    image = image or _image_for(res)
    if not await d.image_exists(image):
        raise DockerError(f"image {image} isn't on this host yet: run `docker compose --profile images "
                          "build`, then Recreate")
    name = res.container or container_name(res)
    if res.kind in EGRESS_KINDS and not res.egress_token:
        res.egress_token = secrets.token_urlsafe(24)
    await d.create(name, spec(res, image, mounts))
    res.container = name
    if res.kind in EGRESS_KINDS and res.network_access:
        await d.connect(settings.resource_network, name, [res.name])
    await d.start(name)
    res.status, res.error = "running", None
    _proxy_current[res.id] = True
    return res


async def recreate(res: Resource, d: Docker | None = None) -> Resource:
    """New container from the current image, same data (e.g. after rebuilding the image)."""
    d = d or docker()
    info = await d.inspect(res.container) if res.container else None
    mounts = _mounts_from(info) if info else None
    if info:
        await d.remove(res.container)
    return await provision(res, d, mounts=mounts)


async def take_over(res: Resource, d: Docker | None = None) -> Resource:
    """Replace a compose-run container with one Agent HQ manages, keeping its data."""
    d = d or docker()
    info = await d.inspect(res.container) if res.container else None
    if info is None:
        found = await d.compose_container(res.name)
        info = await d.inspect(found["Id"]) if found else None
    if info is None:
        raise DockerError(f"no container found for {res.name}")
    mounts = _mounts_from(info)
    image = _image_for(res)
    if not await d.image_exists(image):
        image = (info.get("Config") or {}).get("Image") or image
    await d.remove(info["Id"])
    res.container = container_name(res)
    res.managed = True
    try:
        return await provision(res, d, mounts=mounts, image=image)
    except DockerError:
        res.status = "missing"
        raise


async def set_network_access(res: Resource, on: bool, d: Docker | None = None) -> None:
    d = d or docker()
    if res.kind not in EGRESS_KINDS or not res.container or not d.enabled:
        return
    if on:
        await d.connect(settings.resource_network, res.container, [res.name])
    else:
        await d.disconnect(settings.resource_network, res.container)


async def destroy(res: Resource, d: Docker | None = None, *, remove_data: bool = False) -> None:
    """Remove a managed resource's container (and its volume, if asked)."""
    d = d or docker()
    if not res.managed or not d.enabled:
        return
    if res.container:
        await d.remove(res.container)
    if remove_data:
        vol = await d.volume(volume_name(res))
        if vol and (vol.get("Labels") or {}).get("agent-hq.name") == res.name:
            await d.remove_volume(volume_name(res))


# --------------------------------------------------------------------------- tool servers


def servers_of(s: Session, res_id: str) -> list[ToolServer]:
    return list(s.exec(select(ToolServer).where(ToolServer.resource_id == res_id)
                       .order_by(ToolServer.name)).all())


def make_servers(s: Session, res: Resource) -> list[ToolServer]:
    """The tool servers a new resource serves, registered under its name."""
    if res.kind == "desktop":
        rows = [ToolServer(name=res.name, description=f"{res.name}: screenshots, mouse, keyboard",
                           url=f"http://{res.name}:{DESKTOP_MCP_PORT}/mcp", desktop=True,
                           resource_id=res.id),
                ToolServer(name=f"{res.name}-browser",
                           description=f"{res.name}: its Chromium, via Playwright",
                           url=f"http://{res.name}:{BROWSER_MCP_PORT}/mcp", desktop=True,
                           prefix_tools=False, resource_id=res.id)]
    else:
        rows = [ToolServer(name=res.name, description=f"{res.name}: Python venv, files and shell",
                           url=f"http://{res.name}:{WORKBENCH_MCP_PORT}/mcp", resource_id=res.id)]
    for t in rows:
        if s.exec(select(ToolServer).where(ToolServer.name == t.name)).first():
            raise HTTPException(409, f"a tool server named {t.name} already exists")
        s.add(t)
    return rows


def _host(srv: ToolServer) -> str:
    return (urlparse(srv.url or "").hostname or "") if srv.url else ""


def next_name(s: Session, kind: str, also_taken: set[str] = frozenset()) -> str:
    taken = set(also_taken) | {r.name for r in s.exec(select(Resource)).all()}
    for srv in s.exec(select(ToolServer)).all():
        taken |= {srv.name, _host(srv)}
    n = 1
    while f"{kind}-{n}" in taken:
        n += 1
    return f"{kind}-{n}"


# --------------------------------------------------------------------------- assignment


def holders(s: Session, res_id: str) -> list[AgentDef]:
    """Your agents (not sub-agents) that have this resource."""
    ids = {t.id for t in servers_of(s, res_id)}
    return [a for a in s.exec(select(AgentDef).where(AgentDef.is_subagent == False)  # noqa: E712
                              .order_by(AgentDef.name)).all()
            if ids & set(a.tool_server_ids or [])]


def check_agent_tools(s: Session, agent_id: str | None, tool_server_ids: list[str]) -> list[str]:
    """Validate an agent's ticked servers against resources; returns them normalized.

    Ticking one of a resource's servers gives the agent the whole resource (a desktop's
    screen and browser servers go together). A dedicated resource that is already another
    agent's is refused.
    """
    ids = list(dict.fromkeys(tool_server_ids))
    by_res: dict[str, list[ToolServer]] = {}
    for srv in s.exec(select(ToolServer).where(ToolServer.resource_id != None)).all():  # noqa: E711
        by_res.setdefault(srv.resource_id, []).append(srv)
    for rid, servers in by_res.items():
        if not {t.id for t in servers} & set(ids):
            continue
        res = s.get(Resource, rid)
        if res is None:
            continue
        if res.scope == "dedicated":
            others = [a.name for a in holders(s, rid) if a.id != agent_id]
            if others:
                raise HTTPException(409, f"{res.name} is dedicated to {others[0]}. Make it shared on "
                                         "the Resources page, or give this agent its own.")
        ids += [t.id for t in servers if t.id not in ids]
    return ids


def desktop_url_for(s: Session, tool_server_ids: list[str], current: str | None) -> str | None:
    """The agent's viewer URL: kept if set, else its (first) desktop resource's viewer.

    A viewer of a desktop resource the agent no longer has is dropped.
    """
    if current:
        stale = next((r for r in s.exec(select(Resource).where(Resource.kind == "desktop")).all()
                      if viewer_url(r) == current), None)
        if stale is None or {t.id for t in servers_of(s, stale.id)} & set(tool_server_ids):
            return current
        current = None
    for i in tool_server_ids:
        srv = s.get(ToolServer, i)
        if srv and srv.resource_id and srv.desktop:
            res = s.get(Resource, srv.resource_id)
            if res and res.kind == "desktop":
                return viewer_url(res)
    return current


def assign(s: Session, res: Resource, agent_ids: list[str]) -> list[AgentDef]:
    """Give the resource to exactly these agents (and take it from the rest)."""
    agent_ids = list(dict.fromkeys(agent_ids))
    if res.scope == "dedicated" and len(agent_ids) > 1:
        raise HTTPException(409, f"{res.name} is dedicated: give it to one agent, or make it shared")
    server_ids = [t.id for t in servers_of(s, res.id)]
    viewer = viewer_url(res)
    wanted = []
    for aid in agent_ids:
        a = s.get(AgentDef, aid)
        if not a or a.is_subagent:
            raise HTTPException(404, f"agent {aid} not found")
        wanted.append(a)
    for a in holders(s, res.id):
        if a.id not in agent_ids:
            a.tool_server_ids = [i for i in a.tool_server_ids if i not in server_ids]
            if viewer and a.desktop_url == viewer:
                a.desktop_url = desktop_url_for(s, a.tool_server_ids, None)
            a.updated_at = _now()
            s.add(a)
    for a in wanted:
        a.tool_server_ids = list(a.tool_server_ids or []) + [i for i in server_ids
                                                             if i not in (a.tool_server_ids or [])]
        if viewer and not a.desktop_url:
            a.desktop_url = viewer
        a.updated_at = _now()
        s.add(a)
    return wanted


def unassign_all(s: Session, res: Resource) -> None:
    assign(s, res, [])


def shared_desktops(server_ids: list[str]) -> dict[str, str]:
    """{tool server id: resource id} for servers of shared desktops among these."""
    from . import db

    if not server_ids:
        return {}
    with db.session() as s:
        servers = s.exec(select(ToolServer).where(ToolServer.id.in_(server_ids))).all()
        out = {}
        for srv in servers:
            if srv.resource_id and srv.desktop:
                res = s.get(Resource, srv.resource_id)
                if res and res.scope == "shared":
                    out[srv.id] = res.id
        return out


# --------------------------------------------------------------------------- adoption


async def adoptable(s: Session, d: Docker | None = None) -> list[dict[str, Any]]:
    """desktop-N / workbench-N you run yourself that aren't resources yet.

    Found from registered tool servers whose address is such a host, and (with Docker
    access) from compose services with such names.
    """
    groups: dict[str, list[ToolServer]] = {}
    for srv in s.exec(select(ToolServer).where(ToolServer.resource_id == None)).all():  # noqa: E711
        host = _host(srv)
        if NAME_RE.match(host):
            groups.setdefault(host, []).append(srv)
    known = {r.name for r in s.exec(select(Resource)).all()}
    d = d or docker()
    if d.enabled:
        try:
            for name in await d.compose_services():
                groups.setdefault(name, [])
        except DockerError as e:
            log.warning("can't list compose services: %s", e)
    out = []
    for name, servers in sorted(groups.items()):
        if name in known:
            continue
        ids = {t.id for t in servers}
        agents = [a.name for a in s.exec(select(AgentDef).where(AgentDef.is_subagent == False)).all()  # noqa: E712
                  if ids & set(a.tool_server_ids or [])]
        out.append({"name": name, "kind": NAME_RE.match(name).group(1),
                    "servers": [t.name for t in servers], "agents": agents})
    return out


async def adopt(s: Session, d: Docker | None = None) -> list[Resource]:
    """Make each adoptable container a resource (managed=False), linking its tool servers."""
    d = d or docker()
    made = []
    for item in await adoptable(s, d):
        servers = [t for t in s.exec(select(ToolServer).where(ToolServer.resource_id == None)).all()  # noqa: E711
                   if _host(t) == item["name"]]
        res = Resource(name=item["name"], kind=item["kind"], managed=False,
                       scope="shared" if len(item["agents"]) > 1 else "dedicated",
                       description="adopted from docker-compose.yml")
        s.add(res)
        s.flush()
        if servers:
            for t in servers:
                t.resource_id = res.id
                s.add(t)
        else:
            make_servers(s, res)
        if d.enabled:
            try:
                found = await d.compose_container(res.name)
                res.container = (found["Names"][0].lstrip("/") if found and found.get("Names") else "")
                if res.kind == "workbench" and found:
                    nets = (found.get("NetworkSettings") or {}).get("Networks") or {}
                    res.network_access = settings.resource_network in nets
            except DockerError as e:
                res.error = str(e)
        await refresh(res, d)
        made.append(res)
    return made


# --------------------------------------------------------------------------- leases


def _lease_key(res_id: str) -> str:
    return f"ahq:resource:{res_id}:lease"


async def acquire_lease(r, res_id: str, *, thread_id: str, job_id: str | None, agent: str,
                        on_wait=None, wait: float | None = None) -> dict | None:
    """Take (or keep) a shared desktop for this conversation.

    Returns None when it's ours, else who has it (after waiting up to `wait` seconds).
    A conversation that already has it keeps it: delegated agents in the same chat work
    on the same desktop without waiting for each other.
    """
    wait = settings.desktop_lease_wait if wait is None else wait
    ttl = max(5, int(settings.desktop_lease_ttl))
    me = json.dumps({"thread_id": thread_id, "job_id": job_id, "agent": agent, "since": time.time()})
    key = _lease_key(res_id)
    deadline = time.monotonic() + wait
    told = False
    while True:
        if await r.set(key, me, nx=True, ex=ttl):
            return None
        raw = await r.get(key)
        cur = json.loads(raw) if raw else None
        if cur is None:
            continue  # it just expired; try again
        if cur.get("thread_id") == thread_id:
            await r.set(key, json.dumps({**cur, "job_id": job_id, "agent": agent}), ex=ttl)
            return None
        if time.monotonic() >= deadline:
            return cur
        if not told and on_wait is not None:
            told = True
            await on_wait(cur)
        await asyncio.sleep(min(2.0, max(0.05, deadline - time.monotonic())))


async def release_lease(r, res_id: str, job_id: str | None) -> None:
    """Let go of a shared desktop, if this job holds it."""
    key = _lease_key(res_id)
    raw = await r.get(key)
    if raw and json.loads(raw).get("job_id") == job_id:
        await r.delete(key)


async def lease_holder(r, res_id: str) -> dict | None:
    raw = await r.get(_lease_key(res_id))
    return json.loads(raw) if raw else None


# --------------------------------------------------------------------------- egress rules

EGRESS_KEY = "ahq:egress:config"         # read by the egress proxy every few seconds
EGRESS_BLOCKED = "ahq:egress:blocked"    # what it refused (a stream)
EGRESS_SETTING = "egress"                # SystemSetting: {"defaults": [...]}
DEFAULT_EGRESS = ["pypi.org:443", "files.pythonhosted.org:443"]
_LABEL = re.compile(r"^[a-z0-9-]{1,63}$")


def check_rule(text: str) -> str:
    """A rule as the proxy reads it (see deploy/egress/egress_proxy.py), tidied, or 422."""
    raw = text.strip()
    hostpart, port = raw, ""
    if raw.startswith("["):
        end = raw.find("]")
        hostpart, port = (raw[1:end], raw[end + 2:]) if end > 0 and raw[end + 1:end + 2] in (":", "") else ("", "")
    elif raw.count(":") == 1:
        hostpart, port = raw.split(":")
    host = hostpart.strip().lower().rstrip(".")
    ok_port = port == "" or port == "*" or (port.isdigit() and 0 < int(port) < 65536)
    ok_host = host == "*"
    if not ok_host and "/" in host:
        try:
            ipaddress.ip_network(host, strict=False)
            ok_host = True
        except ValueError:
            pass
    if not ok_host:
        try:
            ipaddress.ip_address(host)
            ok_host = True
        except ValueError:
            name = host[2:] if host.startswith("*.") else host
            ok_host = bool(name) and all(_LABEL.match(lab) for lab in name.split("."))
    if not raw or not ok_host or not ok_port:
        raise HTTPException(422, f"{text!r} isn't a rule: use a host (pypi.org), *.domain, host:port "
                                 "(port or *), an address or a network (192.168.10.0/24:443)")
    return raw.lower()


def check_rules(rules: list[str]) -> list[str]:
    return list(dict.fromkeys(check_rule(r) for r in rules if r.strip()))


def egress_defaults(s: Session) -> list[str]:
    from .models import SystemSetting

    row = s.get(SystemSetting, EGRESS_SETTING)
    return list((row.value or {}).get("defaults", DEFAULT_EGRESS)) if row else list(DEFAULT_EGRESS)


def set_egress_defaults(s: Session, rules: list[str]) -> list[str]:
    from .models import SystemSetting

    rules = check_rules(rules)
    row = s.get(SystemSetting, EGRESS_SETTING) or SystemSetting(key=EGRESS_SETTING)
    row.value = {**(row.value or {}), "defaults": rules}
    s.add(row)
    return rules


def egress_config(s: Session) -> dict[str, Any]:
    """What the egress proxy needs: the defaults, and the rules and token hash of each
    workbench and package server (the proxy calls them all workbenches)."""
    wbs = {}
    for res in s.exec(select(Resource).where(Resource.kind.in_(EGRESS_KINDS))).all():
        if res.egress_token:
            wbs[res.name] = {"token_sha256": hashlib.sha256(res.egress_token.encode()).hexdigest(),
                             "rules": list(res.egress_rules or []), "open": bool(res.network_access)}
    return {"defaults": egress_defaults(s), "workbenches": wbs, "updated": time.time()}


async def publish_egress(r, s: Session) -> None:
    """Hand the current rules to the egress proxy (through Valkey). Never fails the caller."""
    try:
        await r.set(EGRESS_KEY, json.dumps(egress_config(s)))
    except Exception as e:
        log.warning("couldn't publish the egress rules: %s", e)


async def blocked_requests(r, workbench: str | None = None, limit: int = 50) -> list[dict]:
    """Recent requests the proxy refused, newest first; for one workbench, or all."""
    try:
        rows = await r.xrevrange(EGRESS_BLOCKED, count=2000)
    except Exception as e:
        log.warning("couldn't read blocked requests: %s", e)
        return []
    out, seen = [], set()
    for _, f in rows:
        f = {(k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
             for k, v in f.items()}
        if workbench and f.get("workbench") != workbench:
            continue
        key = (f.get("workbench"), f.get("host"), f.get("port"))
        if key in seen:  # one row per workbench, host and port: the latest
            continue
        seen.add(key)
        out.append({"workbench": f.get("workbench"), "host": f.get("host"),
                    "port": int(f.get("port") or 0), "reason": f.get("reason", ""),
                    "at": float(f.get("at") or 0)})
        if len(out) >= limit:
            break
    return out
