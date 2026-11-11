"""Inbound webhooks: managing hooks (/api/webhooks, signed in) and receiving
deliveries (POST /hooks/<name>, checked by each hook's own secret)."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from .. import auth, db
from .. import webhooks as wh
from ..config import settings
from ..db import get_session
from ..models import WebhookEndpoint, _now

router = APIRouter(prefix="/api/webhooks", tags=["events"])
public = APIRouter(tags=["events"])


class WebhookIn(BaseModel):
    name: str
    source: str = ""
    auth_mode: str = "token"
    event_header: str = ""
    event_field: str = ""
    enabled: bool = True


class WebhookOut(BaseModel):
    id: str
    name: str
    source: str
    auth_mode: str
    event_header: str
    event_field: str
    enabled: bool
    created_at: datetime
    last_delivery_at: datetime | None
    url: str
    topic_prefix: str
    has_secret: bool
    # Only in the answer to create and rotate; never listed afterwards.
    secret: str | None = None


def _base_url(request: Request) -> str:
    return settings.public_url or str(request.base_url).rstrip("/")


def _out(request: Request, h: WebhookEndpoint, *, with_secret: bool = False) -> WebhookOut:
    return WebhookOut(
        id=h.id, name=h.name, source=h.source, auth_mode=h.auth_mode, event_header=h.event_header,
        event_field=h.event_field, enabled=h.enabled, created_at=h.created_at,
        last_delivery_at=h.last_delivery_at, url=f"{_base_url(request)}/hooks/{h.name}",
        topic_prefix=f"webhook/{wh._topic_level(h.source or h.name)}/", has_secret=bool(h.secret),
        secret=h.secret if with_secret else None)


def _validate(s: Session, body: WebhookIn, own_id: str | None = None) -> None:
    body.name = body.name.strip().lower()
    body.source = body.source.strip()
    body.event_header = body.event_header.strip()
    body.event_field = body.event_field.strip()
    if not wh.NAME_RE.match(body.name):
        raise HTTPException(422, "name: lowercase letters, digits and dashes (it becomes /hooks/<name>)")
    if body.auth_mode not in wh.AUTH_MODES:
        raise HTTPException(422, f"auth mode must be one of {', '.join(wh.AUTH_MODES)}")
    other = s.exec(select(WebhookEndpoint).where(WebhookEndpoint.name == body.name)).first()
    if other and other.id != own_id:
        raise HTTPException(409, f"a webhook named '{body.name}' already exists")


@router.get("", response_model=list[WebhookOut])
def list_webhooks(request: Request, s: Session = Depends(get_session)):
    rows = s.exec(select(WebhookEndpoint).order_by(WebhookEndpoint.name)).all()
    return [_out(request, h) for h in rows]


@router.post("", response_model=WebhookOut, status_code=201)
def create_webhook(body: WebhookIn, request: Request, s: Session = Depends(get_session)):
    _validate(s, body)
    h = WebhookEndpoint(**body.model_dump(), secret=wh.new_secret())
    s.add(h)
    s.commit()
    s.refresh(h)
    return _out(request, h, with_secret=True)


@router.put("/{hook_id}", response_model=WebhookOut)
def update_webhook(hook_id: str, body: WebhookIn, request: Request, s: Session = Depends(get_session)):
    h = s.get(WebhookEndpoint, hook_id)
    if not h:
        raise HTTPException(404)
    _validate(s, body, hook_id)
    for k, v in body.model_dump().items():
        setattr(h, k, v)
    s.add(h)
    s.commit()
    s.refresh(h)
    return _out(request, h)


@router.post("/{hook_id}/rotate", response_model=WebhookOut)
def rotate_secret(hook_id: str, request: Request, s: Session = Depends(get_session)):
    """A new secret; the old one stops working at once."""
    h = s.get(WebhookEndpoint, hook_id)
    if not h:
        raise HTTPException(404)
    h.secret = wh.new_secret()
    s.add(h)
    s.commit()
    s.refresh(h)
    return _out(request, h, with_secret=True)


@router.delete("/{hook_id}", status_code=204)
def delete_webhook(hook_id: str, s: Session = Depends(get_session)):
    h = s.get(WebhookEndpoint, hook_id)
    if not h:
        raise HTTPException(404)
    s.delete(h)
    s.commit()


# --------------------------------------------------------------------------- deliveries


async def _read_body(request: Request) -> bytes:
    limit = settings.webhook_max_body
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, f"body larger than {limit} bytes")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, f"body larger than {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


@public.post("/hooks/{name}", status_code=202)
async def receive(name: str, request: Request):
    """One delivery. 202 as soon as it is routed; agents work on it afterwards."""
    with db.session() as s:
        hook = s.exec(select(WebhookEndpoint).where(WebhookEndpoint.name == name.lower())).first()
    # Unknown and disabled hooks look the same, so names can't be probed.
    if hook is None or not hook.enabled:
        raise HTTPException(404, "No such webhook")
    body = await _read_body(request)
    headers = {k.lower(): v for k, v in request.headers.items()}
    if not wh.check_sender(hook, headers, dict(request.query_params), body):
        ip = auth.client_ip(headers, request.client.host if request.client else None)
        auth.record("webhook_rejected", ip=ip, user_agent=headers.get("user-agent", ""),
                    detail=f"hook {hook.name}: missing or wrong {'signature' if hook.auth_mode == 'gitea' else 'token'}")
        raise HTTPException(401, "Signature or token check failed")
    from ..mqtt_bridge import handle_message

    topic = wh.topic_for(hook, headers, body)
    jobs = await handle_message(request.app.state.runtime, topic, body, source="webhook")
    with db.session() as s:
        row = s.get(WebhookEndpoint, hook.id)
        if row:
            row.last_delivery_at = _now()
            s.add(row)
            s.commit()
    return JSONResponse({"accepted": True, "topic": topic, "queued_jobs": jobs}, status_code=202)
