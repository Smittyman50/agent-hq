"""Resources page: create desktops and workbenches, give them to agents, run their containers.

See app/resources.py for what a resource is.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session, select

from .. import resources as rs
from ..db import get_session
from ..models import Resource, ToolServer, _now

router = APIRouter(prefix="/api/resources", tags=["resources"])


class ResourceIn(BaseModel):
    kind: str = "desktop"          # desktop | workbench
    scope: str = "dedicated"       # dedicated | shared
    description: str = ""
    network_access: bool = False   # workbench: internet and LAN, not only PyPI
    agent_ids: list[str] = []      # give it to these agents straight away


class ResourceUpdate(BaseModel):
    scope: str | None = None
    description: str | None = None
    network_access: bool | None = None
    egress_rules: list[str] | None = None  # workbench: what it may reach besides the defaults


class EgressDefaultsIn(BaseModel):
    defaults: list[str]


class AllowIn(BaseModel):
    rule: str


class AssignIn(BaseModel):
    agent_ids: list[str]


def _check(kind: str | None = None, scope: str | None = None) -> None:
    if kind is not None and kind not in rs.KINDS:
        raise HTTPException(422, "kind must be desktop or workbench")
    if scope is not None and scope not in rs.SCOPES:
        raise HTTPException(422, "scope must be dedicated or shared")


def _get(s: Session, res_id: str) -> Resource:
    res = s.get(Resource, res_id)
    if not res:
        raise HTTPException(404, "resource not found")
    return res


async def _view(s: Session, res: Resource, request: Request | None = None) -> dict[str, Any]:
    lease = None
    if request is not None and res.kind == "desktop" and res.scope == "shared":
        try:
            lease = await rs.lease_holder(request.app.state.broker.r, res.id)
        except Exception:  # Valkey down: the page still works
            lease = None
    return {**res.model_dump(exclude={"egress_token"}),
            "egress_rules": list(res.egress_rules or []),
            "proxy_current": rs.proxy_current(res),
            "viewer_url": rs.viewer_url(res),
            "servers": [{"id": t.id, "name": t.name, "url": t.url, "desktop": t.desktop}
                        for t in rs.servers_of(s, res.id)],
            "agents": [{"id": a.id, "name": a.name} for a in rs.holders(s, res.id)],
            "in_use_by": lease}


def _fail(e: rs.DockerError) -> HTTPException:
    return HTTPException(502, str(e))


@router.get("")
async def list_resources(request: Request, s: Session = Depends(get_session)) -> dict[str, Any]:
    d = rs.docker()
    rows = s.exec(select(Resource).order_by(Resource.kind, Resource.name)).all()
    for res in rows:
        await rs.refresh(res, d)
        s.add(res)
    s.commit()
    await rs.publish_egress(request.app.state.broker.r, s)
    return {"docker": d.enabled,
            "resources": [await _view(s, r, request) for r in rows],
            "adoptable": await rs.adoptable(s, d),
            "egress_defaults": rs.egress_defaults(s)}


@router.get("/network")
async def network(request: Request, s: Session = Depends(get_session)) -> dict[str, Any]:
    """The rules every workbench gets through the egress proxy, and what it refused lately."""
    return {"defaults": rs.egress_defaults(s),
            "blocked": await rs.blocked_requests(request.app.state.broker.r)}


@router.put("/network")
async def set_network(body: EgressDefaultsIn, request: Request, s: Session = Depends(get_session)):
    rules = rs.set_egress_defaults(s, body.defaults)
    s.commit()
    await rs.publish_egress(request.app.state.broker.r, s)
    return {"defaults": rules, "blocked": await rs.blocked_requests(request.app.state.broker.r)}


@router.post("", status_code=201)
async def create_resource(body: ResourceIn, request: Request, s: Session = Depends(get_session)):
    _check(body.kind, body.scope)
    d = rs.docker()
    taken: set[str] = set()
    if d.enabled:
        try:
            taken = set(await d.compose_services())
        except rs.DockerError as e:
            raise _fail(e) from e
    res = Resource(name=rs.next_name(s, body.kind, taken), kind=body.kind, scope=body.scope,
                   description=body.description,
                   network_access=body.network_access and body.kind == "workbench")
    s.add(res)
    s.flush()
    rs.make_servers(s, res)
    rs.assign(s, res, body.agent_ids)
    if d.enabled:
        try:
            await rs.provision(res, d)
        except rs.DockerError as e:
            res.status, res.error = "error", str(e)
    else:
        res.status = "unknown"
        res.error = ("Agent HQ has no Docker access, so it registered the tool servers but "
                     "created no container. See the README's Resources section.")
    s.add(res)
    s.commit()
    s.refresh(res)
    await rs.publish_egress(request.app.state.broker.r, s)
    return await _view(s, res, request)


@router.put("/{res_id}")
async def update_resource(res_id: str, body: ResourceUpdate, request: Request,
                          s: Session = Depends(get_session)):
    res = _get(s, res_id)
    _check(scope=body.scope)
    if body.scope == "dedicated" and len(rs.holders(s, res.id)) > 1:
        raise HTTPException(409, f"{res.name} is used by several agents; take it from all but one first")
    if body.scope is not None:
        res.scope = body.scope
    if body.description is not None:
        res.description = body.description
    if body.network_access is not None and res.kind == "workbench" and body.network_access != res.network_access:
        try:
            await rs.set_network_access(res, body.network_access)
        except rs.DockerError as e:
            raise _fail(e) from e
        res.network_access = body.network_access
    if body.egress_rules is not None:
        if res.kind != "workbench":
            raise HTTPException(422, "only workbenches have egress rules")
        res.egress_rules = rs.check_rules(body.egress_rules)
    res.updated_at = _now()
    s.add(res)
    s.commit()
    await rs.publish_egress(request.app.state.broker.r, s)
    return await _view(s, res, request)


@router.put("/{res_id}/agents")
async def assign_resource(res_id: str, body: AssignIn, request: Request,
                          s: Session = Depends(get_session)):
    res = _get(s, res_id)
    rs.assign(s, res, body.agent_ids)
    s.commit()
    return await _view(s, res, request)


async def _container_action(res_id: str, action: str, request: Request, s: Session):
    res = _get(s, res_id)
    d = rs.docker()
    try:
        if action == "take-over":
            if res.managed:
                raise HTTPException(409, f"Agent HQ already manages {res.name}")
            await rs.take_over(res, d)
        else:
            if action != "recreate" and not res.container:
                raise HTTPException(409, f"{res.name} has no container"
                                    + (" yet" if res.managed else " found in docker compose"))
            if not res.managed and action == "recreate":
                raise HTTPException(409, f"docker compose runs {res.name}; take it over first")
            if action == "start":
                await d.start(res.container)
            elif action == "stop":
                await d.stop(res.container)
            elif action == "restart":
                await d.restart(res.container)
            elif action == "recreate":
                await rs.recreate(res, d)
        await rs.refresh(res, d)
    except rs.DockerError as e:
        res.error = str(e)
        s.add(res)
        s.commit()
        raise _fail(e) from e
    res.updated_at = _now()
    s.add(res)
    s.commit()
    await rs.publish_egress(request.app.state.broker.r, s)  # a new container may have a new token
    return await _view(s, res, request)


@router.get("/{res_id}/blocked")
async def blocked(res_id: str, request: Request, s: Session = Depends(get_session)):
    """What the egress proxy refused this workbench lately (newest first)."""
    res = _get(s, res_id)
    return await rs.blocked_requests(request.app.state.broker.r, res.name)


@router.post("/{res_id}/allow")
async def allow(res_id: str, body: AllowIn, request: Request, s: Session = Depends(get_session)):
    """Add one rule to a workbench (the Allow button next to a blocked request)."""
    res = _get(s, res_id)
    if res.kind != "workbench":
        raise HTTPException(422, "only workbenches have egress rules")
    res.egress_rules = rs.check_rules(list(res.egress_rules or []) + [body.rule])
    res.updated_at = _now()
    s.add(res)
    s.commit()
    await rs.publish_egress(request.app.state.broker.r, s)
    return await _view(s, res, request)


@router.post("/{res_id}/detach", status_code=204)
def detach(res_id: str, s: Session = Depends(get_session)):
    """Forget an adopted resource but keep its tool servers (as plain tool servers)."""
    res = _get(s, res_id)
    if res.managed:
        raise HTTPException(409, "only adopted resources can be detached; delete this one instead")
    for t in s.exec(select(ToolServer).where(ToolServer.resource_id == res.id)).all():
        t.resource_id = None
        s.add(t)
    s.delete(res)
    s.commit()


@router.post("/{res_id}/{action}")
async def container_action(res_id: str, action: str, request: Request,
                           s: Session = Depends(get_session)):
    """start | stop | restart | recreate (new container, same data) | take-over (from compose)."""
    if action not in ("start", "stop", "restart", "recreate", "take-over"):
        raise HTTPException(404)
    return await _container_action(res_id, action, request, s)


@router.post("/adopt")
async def adopt(request: Request, s: Session = Depends(get_session)):
    """Make the desktops and workbenches you run in docker-compose.yml resources."""
    made = await rs.adopt(s)
    s.commit()
    return [await _view(s, r, request) for r in made]


@router.delete("/{res_id}", status_code=204)
async def delete_resource(res_id: str, request: Request, remove_data: bool = False,
                          s: Session = Depends(get_session)):
    """Take it from every agent and delete its tool servers. A managed resource's container
    goes too (its data volume only with remove_data). An adopted one's container is left
    to docker compose."""
    res = _get(s, res_id)
    try:
        await rs.destroy(res, remove_data=remove_data)
    except rs.DockerError as e:
        raise _fail(e) from e
    rs.unassign_all(s, res)
    for t in rs.servers_of(s, res.id):
        s.delete(t)
    s.delete(res)
    s.commit()
    await rs.publish_egress(request.app.state.broker.r, s)
