"""Inbound webhooks: managing hooks (/api/webhooks, signed in) and receiving
deliveries (POST /hooks/<name>, checked by each hook's own secret)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from .. import auth, db
from .. import webhooks as wh
from ..config import settings
from ..db import get_session
from ..models import WebhookDelivery, WebhookEndpoint, _now
from ..telemetry import delivery_span

router = APIRouter(prefix="/api/webhooks", tags=["events"])
public = APIRouter(tags=["events"])


class WebhookIn(BaseModel):
    name: str
    source: str = ""
    auth_mode: str = "token"
    event_header: str = ""
    event_field: str = ""
    delivery_header: str = ""
    allowed_networks: str = ""
    rate_per_minute: int | None = None  # None: the default for a new hook, unchanged on edit
    cautious: bool = True
    enabled: bool = True


class WebhookOut(BaseModel):
    id: str
    name: str
    source: str
    auth_mode: str
    event_header: str
    event_field: str
    delivery_header: str
    allowed_networks: str
    rate_per_minute: int
    cautious: bool
    enabled: bool
    created_at: datetime
    last_delivery_at: datetime | None
    url: str
    topic_prefix: str
    has_secret: bool
    encrypted: bool
    # Only in the answer to create and rotate; never listed afterwards.
    secret: str | None = None


def _base_url(request: Request) -> str:
    return settings.public_url or str(request.base_url).rstrip("/")


def _out(request: Request, h: WebhookEndpoint, *, secret: str | None = None) -> WebhookOut:
    return WebhookOut(
        id=h.id, name=h.name, source=h.source, auth_mode=h.auth_mode, event_header=h.event_header,
        event_field=h.event_field, delivery_header=h.delivery_header,
        allowed_networks=h.allowed_networks, rate_per_minute=h.rate_per_minute, cautious=h.cautious,
        enabled=h.enabled, created_at=h.created_at,
        last_delivery_at=h.last_delivery_at, url=f"{_base_url(request)}/hooks/{h.name}",
        topic_prefix=f"webhook/{wh._topic_level(h.source or h.name)}/", has_secret=bool(h.secret),
        encrypted=h.secret.startswith(wh.SEALED), secret=secret)


def _validate(s: Session, body: WebhookIn, own_id: str | None = None) -> None:
    body.name = body.name.strip().lower()
    body.source = body.source.strip()
    body.event_header = body.event_header.strip()
    body.event_field = body.event_field.strip()
    body.delivery_header = body.delivery_header.strip()
    body.allowed_networks = ", ".join(p for p in re.split(r"[,\s]+", body.allowed_networks.strip()) if p)
    try:
        wh.parse_networks(body.allowed_networks)
    except ValueError as e:
        raise HTTPException(422, f"allowed networks: {e}")
    if body.rate_per_minute is not None and body.rate_per_minute < 0:
        raise HTTPException(422, "rate per minute: 0 (no limit) or more")
    if not wh.NAME_RE.match(body.name):
        raise HTTPException(422, "name: lowercase letters, digits and dashes (it becomes /hooks/<name>)")
    if body.auth_mode not in wh.AUTH_MODES:
        raise HTTPException(422, f"auth mode must be one of {', '.join(wh.AUTH_MODES)}")
    other = s.exec(select(WebhookEndpoint).where(WebhookEndpoint.name == body.name)).first()
    if other and other.id != own_id:
        raise HTTPException(409, f"a webhook named '{body.name}' already exists")


@router.get("/settings")
def webhook_settings():
    """Server-wide webhook settings (from .env), for the Events page."""
    return {"encryption": wh.encryption_on(), "default_networks": settings.webhook_networks,
            "default_rate_per_minute": settings.webhook_rate_per_minute,
            "keep_days": settings.webhook_keep_days, "keep_per_hook": settings.webhook_keep_deliveries,
            "dedupe_hours": settings.webhook_dedupe_seconds / 3600,
            "max_body": settings.webhook_max_body}


@router.get("", response_model=list[WebhookOut])
def list_webhooks(request: Request, s: Session = Depends(get_session)):
    rows = s.exec(select(WebhookEndpoint).order_by(WebhookEndpoint.name)).all()
    return [_out(request, h) for h in rows]


@router.post("", response_model=WebhookOut, status_code=201)
def create_webhook(body: WebhookIn, request: Request, s: Session = Depends(get_session)):
    _validate(s, body)
    fields = body.model_dump()
    if fields["rate_per_minute"] is None:
        fields["rate_per_minute"] = settings.webhook_rate_per_minute
    secret = wh.new_secret()
    h = WebhookEndpoint(**fields, secret=wh.seal(secret))
    s.add(h)
    s.commit()
    s.refresh(h)
    return _out(request, h, secret=secret)


@router.put("/{hook_id}", response_model=WebhookOut)
def update_webhook(hook_id: str, body: WebhookIn, request: Request, s: Session = Depends(get_session)):
    h = s.get(WebhookEndpoint, hook_id)
    if not h:
        raise HTTPException(404)
    _validate(s, body, hook_id)
    for k, v in body.model_dump().items():
        if k == "rate_per_minute" and v is None:
            continue
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
    secret = wh.new_secret()
    h.secret = wh.seal(secret)
    s.add(h)
    s.commit()
    s.refresh(h)
    return _out(request, h, secret=secret)


@router.delete("/{hook_id}", status_code=204)
def delete_webhook(hook_id: str, s: Session = Depends(get_session)):
    h = s.get(WebhookEndpoint, hook_id)
    if not h:
        raise HTTPException(404)
    for d in s.exec(select(WebhookDelivery).where(WebhookDelivery.hook_id == hook_id)).all():
        s.delete(d)
    s.delete(h)
    s.commit()


# --------------------------------------------------------------------------- deliveries


class TooLarge(Exception):
    pass


async def _read_body(request: Request) -> bytes:
    limit = settings.webhook_max_body
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise TooLarge(int(declared))
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise TooLarge(size)
        chunks.append(chunk)
    return b"".join(chunks)


def _status(outcomes: list[dict]) -> str:
    if any(o["result"] == "queued" for o in outcomes):
        return "accepted"
    return "cooldown" if outcomes else "no_match"


def log_delivery(hook: WebhookEndpoint, status: str, *, topic: str = "", event: str = "",
                 ip: str = "", delivery_id: str = "", headers: dict[str, str] | None = None,
                 body: bytes | None = None, body_bytes: int | None = None,
                 outcomes: list[dict] | None = None, test: bool = False,
                 replay_of: str | None = None, detail: str = "") -> WebhookDelivery:
    """Record one delivery, keep the newest AHQ_WEBHOOK_KEEP_DELIVERIES for the hook, and
    note the hook's last delivery."""
    with db.session() as s:
        row = WebhookDelivery(
            hook_id=hook.id, status=status, topic=topic, event=event, ip=ip[:64],
            delivery_id=delivery_id, headers=wh.safe_headers(headers or {}),
            body=None if body is None else body.decode("utf-8", errors="replace"),
            body_bytes=len(body) if body is not None else (body_bytes or 0),
            outcomes=outcomes or [], test=test, replay_of=replay_of, detail=detail[:500])
        s.add(row)
        h = s.get(WebhookEndpoint, hook.id)
        if h:
            h.last_delivery_at = row.at
            s.add(h)
        s.commit()
        s.refresh(row)
        old = s.exec(select(WebhookDelivery.id).where(WebhookDelivery.hook_id == hook.id)
                     .order_by(WebhookDelivery.at.desc(), WebhookDelivery.id.desc())
                     .offset(max(settings.webhook_keep_deliveries, 1))).all()
        for did in old:
            d = s.get(WebhookDelivery, did)
            if d:
                s.delete(d)
        if old:
            s.commit()
            s.refresh(row)
        s.expunge(row)
        return row


async def _route(request: Request, hook: WebhookEndpoint, topic: str, body: bytes,
                 skip_cooldown: bool = False) -> list[dict]:
    from ..mqtt_bridge import route_event

    ctx = {"source": topic.split("/")[1], "event": topic.rsplit("/", 1)[-1], "hook": hook.name}
    return await route_event(request.app.state.runtime, topic, body, "webhook", ctx=ctx,
                             skip_cooldown=skip_cooldown, cautious=hook.cautious)


def _reply(row: WebhookDelivery, status_code: int = 202, **extra: Any) -> JSONResponse:
    return JSONResponse(
        {"accepted": status_code == 202, "delivery": row.id, "status": row.status, "topic": row.topic,
         "queued_jobs": [o["job_id"] for o in row.outcomes if o.get("job_id")], **extra},
        status_code=status_code, headers={"x-ahq-status": row.status})


@public.post("/hooks/{name}", status_code=202)
async def receive(name: str, request: Request):
    """One delivery. 202 as soon as it is routed; agents work on it afterwards."""
    with db.session() as s:
        hook = s.exec(select(WebhookEndpoint).where(WebhookEndpoint.name == name.lower())).first()
    # Unknown and disabled hooks look the same, so names can't be probed.
    if hook is None or not hook.enabled:
        raise HTTPException(404, "No such webhook")
    headers = {k.lower(): v for k, v in request.headers.items()}
    ip = auth.client_ip(headers, request.client.host if request.client else None)
    with delivery_span(hook.name, ip) as span:
        reply = await _receive(request, hook, headers, ip, span)
        span.set_attribute("agent_hq.webhook.status", reply.headers.get("x-ahq-status", ""))
        return reply


async def _receive(request: Request, hook: WebhookEndpoint, headers: dict[str, str], ip: str,
                   span) -> JSONResponse:
    def refuse(status: str, code: int, message: str, **log_kw: Any) -> JSONResponse:
        log_delivery(hook, status, ip=ip, headers=headers, **log_kw)
        return JSONResponse({"detail": message}, status_code=code, headers={"x-ahq-status": status})

    if not wh.address_allowed(hook, ip):
        return refuse("blocked", 403, "Not accepted from this address",
                      detail=f"{ip or 'unknown address'} isn't in the allowed networks")
    over = await wh.over_rate(request.app.state.runtime.broker, hook)
    if over:
        if over == hook.rate_per_minute + 1:  # log the first one over, not the whole flood
            log_delivery(hook, "rate_limited", ip=ip, headers=headers,
                         detail=f"over {hook.rate_per_minute} a minute; more this minute aren't logged")
        return JSONResponse({"detail": "Too many requests"}, status_code=429,
                            headers={"retry-after": "60", "x-ahq-status": "rate_limited"})
    try:
        body = await _read_body(request)
    except TooLarge as e:
        return refuse("too_large", 413, f"body larger than {settings.webhook_max_body} bytes",
                      body_bytes=e.args[0], detail=f"over {settings.webhook_max_body} bytes")
    try:
        ok = wh.check_sender(hook, headers, dict(request.query_params), body)
    except wh.SecretUnavailable as e:
        return refuse("rejected", 503, "This webhook can't check senders right now",
                      body_bytes=len(body), detail=f"can't read the hook's secret: {e}")
    if not ok:
        what = "signature" if hook.auth_mode == "gitea" else "token"
        auth.record("webhook_rejected", ip=ip, user_agent=headers.get("user-agent", ""),
                    detail=f"hook {hook.name}: missing or wrong {what}")
        return refuse("rejected", 401, "Signature or token check failed", body_bytes=len(body),
                      detail=f"missing or wrong {what}")
    topic = wh.topic_for(hook, headers, body)
    event = topic.rsplit("/", 1)[-1]
    span.set_attribute("agent_hq.webhook.topic", topic)
    span.set_attribute("input.value", body[:4000].decode("utf-8", errors="replace"))
    did = wh.delivery_id(hook, headers)
    if did and not await request.app.state.runtime.broker.first_seen(
            f"{hook.id}:{did}", settings.webhook_dedupe_seconds):
        row = log_delivery(hook, "duplicate", topic=topic, event=event, ip=ip, delivery_id=did,
                           headers=headers, body=body, detail="delivery ID seen before")
        return _reply(row, duplicate=True)
    outcomes = await _route(request, hook, topic, body)
    row = log_delivery(hook, _status(outcomes), topic=topic, event=event, ip=ip, delivery_id=did,
                       headers=headers, body=body, outcomes=outcomes)
    span.set_attribute("output.value", ", ".join(
        f"{o['subscription']}: {o['result']}" for o in outcomes) or "no subscription matched")
    return _reply(row)


def delete_old_deliveries(days: float | None = None) -> int:
    days = settings.webhook_keep_days if days is None else days
    if days <= 0:
        return 0
    cutoff = _now() - timedelta(days=days)
    with db.session() as s:
        rows = s.exec(select(WebhookDelivery).where(WebhookDelivery.at < cutoff)).all()
        for r in rows:
            s.delete(r)
        s.commit()
    return len(rows)


async def cleanup_loop(every: float = 3600) -> None:
    """Hourly: remove deliveries older than AHQ_WEBHOOK_KEEP_DAYS."""
    import asyncio
    import logging

    log = logging.getLogger("agent_hq.webhooks")
    while True:
        try:
            n = delete_old_deliveries()
            if n:
                log.info("removed %d webhook deliveries older than %g days", n, settings.webhook_keep_days)
        except Exception:
            log.exception("webhook delivery cleanup failed")
        await asyncio.sleep(every)


# --------------------------------------------------------------------------- log, test, replay


class DeliverySummary(BaseModel):
    id: str
    hook_id: str
    hook: str
    at: datetime
    status: str
    topic: str
    event: str
    ip: str
    delivery_id: str
    body_bytes: int
    has_body: bool
    test: bool
    replay_of: str | None
    detail: str
    subscriptions: list[str]
    queued: int


def _summary(d: WebhookDelivery, names: dict[str, str]) -> DeliverySummary:
    return DeliverySummary(
        id=d.id, hook_id=d.hook_id, hook=names.get(d.hook_id, "?"), at=d.at, status=d.status,
        topic=d.topic, event=d.event, ip=d.ip, delivery_id=d.delivery_id, body_bytes=d.body_bytes,
        has_body=d.body is not None, test=d.test, replay_of=d.replay_of, detail=d.detail,
        subscriptions=[o.get("subscription", "") for o in d.outcomes or []],
        queued=sum(1 for o in d.outcomes or [] if o.get("result") == "queued"))


def _names(s: Session) -> dict[str, str]:
    return {h.id: h.name for h in s.exec(select(WebhookEndpoint)).all()}


@router.get("/deliveries", response_model=list[DeliverySummary])
def recent_deliveries(topic_filter: str = "", with_body: bool = False, limit: int = 50,
                      s: Session = Depends(get_session)):
    """Recent deliveries across all hooks, newest first. `topic_filter` (MQTT wildcards)
    narrows them, e.g. for previewing a subscription's template."""
    from ..mqtt_bridge import topic_matches

    limit = max(min(limit, 500), 1)
    scan = 5000 if (topic_filter or with_body) else limit
    q = select(WebhookDelivery).order_by(WebhookDelivery.at.desc()).limit(scan)
    names = _names(s)
    out = []
    for d in s.exec(q).all():
        if topic_filter and not (d.topic and topic_matches(topic_filter, d.topic)):
            continue
        if with_body and d.body is None:
            continue
        out.append(_summary(d, names))
        if len(out) >= limit:
            break
    return out


@router.get("/deliveries/{delivery_id}")
def delivery_detail(delivery_id: str, s: Session = Depends(get_session)):
    d = s.get(WebhookDelivery, delivery_id)
    if not d:
        raise HTTPException(404)
    return {**_summary(d, _names(s)).model_dump(), "headers": d.headers, "body": d.body,
            "outcomes": d.outcomes}


@router.get("/{hook_id}/deliveries", response_model=list[DeliverySummary])
def hook_deliveries(hook_id: str, limit: int = 50, s: Session = Depends(get_session)):
    if not s.get(WebhookEndpoint, hook_id):
        raise HTTPException(404)
    rows = s.exec(select(WebhookDelivery).where(WebhookDelivery.hook_id == hook_id)
                  .order_by(WebhookDelivery.at.desc(), WebhookDelivery.id.desc())
                  .limit(max(min(limit, 500), 1))).all()
    names = _names(s)
    return [_summary(d, names) for d in rows]


class SendTest(BaseModel):
    event: str = "test"
    body: str = ""
    respect_cooldown: bool = False


@router.post("/{hook_id}/test")
async def send_test(hook_id: str, body: SendTest, request: Request):
    """Route a delivery as this hook would, without a sender (you're signed in)."""
    with db.session() as s:
        hook = s.get(WebhookEndpoint, hook_id)
    if not hook:
        raise HTTPException(404)
    raw = body.body.encode()
    topic = f"webhook/{wh._topic_level(hook.source or hook.name)}/{wh._topic_level(body.event or 'event')}"
    outcomes = await _route(request, hook, topic, raw, skip_cooldown=not body.respect_cooldown)
    row = log_delivery(hook, _status(outcomes), topic=topic, event=topic.rsplit("/", 1)[-1],
                       ip=auth.client_ip({k.lower(): v for k, v in request.headers.items()},
                                         request.client.host if request.client else None),
                       body=raw, outcomes=outcomes, test=True, detail="sent from the Events page")
    return _reply(row, 200)


class ReplayIn(BaseModel):
    respect_cooldown: bool = False


@router.post("/deliveries/{delivery_id}/replay")
async def replay(delivery_id: str, request: Request, body: ReplayIn | None = None):
    """Route a logged delivery again (same topic and body), as a new log row. Skips the
    duplicate check, and the cooldown unless respect_cooldown is set."""
    with db.session() as s:
        d = s.get(WebhookDelivery, delivery_id)
        hook = s.get(WebhookEndpoint, d.hook_id) if d else None
    if not d or not hook:
        raise HTTPException(404)
    if d.body is None or not d.topic:
        raise HTTPException(409, "this delivery was rejected, so its body wasn't kept")
    raw = d.body.encode()
    respect = bool(body and body.respect_cooldown)
    outcomes = await _route(request, hook, d.topic, raw, skip_cooldown=not respect)
    row = log_delivery(hook, _status(outcomes), topic=d.topic, event=d.event, ip=d.ip,
                       delivery_id=d.delivery_id, headers=d.headers, body=raw, outcomes=outcomes,
                       replay_of=d.id, detail="replayed from the Events page")
    return _reply(row, 200)
