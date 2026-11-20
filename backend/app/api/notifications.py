"""Notification channels (MQTT, Telegram), the rules that route notices to them, pairing,
test sends and the delivery log (Settings → Notifications)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, col, select

from .. import notifications as nt
from ..channels import CHAT_KINDS, KINDS, ChannelError, driver_for
from ..db import get_session
from ..models import (AgentDef, ChannelInbound, Notice, NoticeDelivery, NotificationChannel,
                      NotificationRoute)
from ..webhooks import SecretUnavailable, seal

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


class RouteIn(BaseModel):
    types: list[str] = []      # empty = every type
    agent_ids: list[str] = []  # empty = every agent
    min_severity: str = "info"
    enabled: bool = True


class ChannelIn(BaseModel):
    name: str
    kind: str = "telegram"
    enabled: bool = True
    token: str = ""            # Telegram: the bot token from @BotFather; empty on update = keep it
    topic: str = ""            # MQTT: empty = AHQ_MQTT_NOTIFY_TOPIC
    decisions_allowed: bool = True
    default_agent_id: str | None = None
    routes: list[RouteIn] | None = None  # None = keep them (new channel: the defaults)


class SettingsIn(BaseModel):
    alerts_per_hour: int = Field(default=10, ge=0, le=200)
    linger_minutes: int = Field(default=30, ge=0, le=1440)


def _routes(s: Session, channel_id: str) -> list[NotificationRoute]:
    return list(s.exec(select(NotificationRoute).where(NotificationRoute.channel_id == channel_id)
                       .order_by(NotificationRoute.created_at)).all())


def _out(s: Session, ch: NotificationChannel) -> dict[str, Any]:
    cfg = dict(ch.config or {})
    return {
        "id": ch.id, "name": ch.name, "kind": ch.kind, "enabled": ch.enabled,
        "chat": ch.kind in CHAT_KINDS, "decisions_allowed": ch.decisions_allowed,
        "default_agent_id": ch.default_agent_id, "chat_thread_id": ch.chat_thread_id,
        "has_token": bool(cfg.get("token")), "bot_username": cfg.get("bot_username", ""),
        "topic": cfg.get("topic", ""),
        "paired": [{k: p.get(k) for k in ("user_id", "name", "chat_id", "paired_at")} for p in ch.paired or []],
        "pairing_open": bool(ch.pairing_code and ch.pairing_expires_at
                             and nt._aware(ch.pairing_expires_at) > datetime.now(timezone.utc)),
        "last_error": ch.last_error, "last_ok_at": ch.last_ok_at, "created_at": ch.created_at,
        "routes": [r.model_dump() for r in _routes(s, ch.id)],
    }


def _check(s: Session, body: ChannelIn, own: NotificationChannel | None) -> None:
    if not body.name.strip():
        raise HTTPException(422, "give the channel a name")
    if body.kind not in KINDS:
        raise HTTPException(422, f"kind is one of {', '.join(KINDS)}")
    if own is not None and body.kind != own.kind:
        raise HTTPException(422, "a channel's kind can't change: add a new channel")
    other = s.exec(select(NotificationChannel).where(NotificationChannel.name == body.name.strip())).first()
    if other is not None and (own is None or other.id != own.id):
        raise HTTPException(409, f"a channel named '{body.name.strip()}' already exists")
    if body.default_agent_id and not s.get(AgentDef, body.default_agent_id):
        raise HTTPException(422, "default agent not found")
    if body.kind == "telegram" and not body.token.strip() and not (own and (own.config or {}).get("token")):
        raise HTTPException(422, "paste the bot token from @BotFather")
    for r in body.routes or []:
        bad = [t for t in r.types if t not in nt.TYPES]
        if bad:
            raise HTTPException(422, f"unknown notice type: {', '.join(bad)}")
        if r.min_severity not in nt.SEVERITIES:
            raise HTTPException(422, f"min_severity is one of {', '.join(nt.SEVERITIES)}")


async def _verify(ch: NotificationChannel) -> dict[str, Any]:
    """Ask the channel whether its settings work (Telegram: getMe with the token)."""
    try:
        return await driver_for(ch).check()
    except SecretUnavailable as e:
        raise HTTPException(409, str(e)) from None
    except ChannelError as e:
        raise HTTPException(422, f"{ch.kind}: {e}") from None


def _set_routes(s: Session, channel_id: str, routes: list[RouteIn]) -> None:
    for r in _routes(s, channel_id):
        s.delete(r)
    for r in routes:
        s.add(NotificationRoute(channel_id=channel_id, **r.model_dump()))


@router.get("/types")
def types():
    return {"types": [{"type": k, **v} for k, v in nt.TYPES.items()], "severities": list(nt.SEVERITIES),
            "kinds": list(KINDS), "chat_kinds": list(CHAT_KINDS), "default_chat_types": nt.DEFAULT_CHAT_TYPES}


@router.get("/settings")
def get_settings():
    return nt.get_settings()


@router.put("/settings")
def put_settings(body: SettingsIn):
    return nt.save_settings(body.model_dump())


@router.get("/channels")
def list_channels(s: Session = Depends(get_session)):
    nt.ensure_defaults()
    return [_out(s, c) for c in s.exec(select(NotificationChannel).order_by(NotificationChannel.name)).all()]


@router.post("/channels", status_code=201)
async def create_channel(body: ChannelIn, s: Session = Depends(get_session)):
    _check(s, body, None)
    ch = NotificationChannel(name=body.name.strip(), kind=body.kind, enabled=body.enabled,
                             decisions_allowed=body.decisions_allowed and body.kind in CHAT_KINDS,
                             default_agent_id=body.default_agent_id or None)
    if body.kind == "telegram":
        ch.config = {"token": body.token.strip()}
        ch.config = {**ch.config, **(await _verify(ch))}
        ch.config["token"] = seal(ch.config["token"])
    else:
        ch.config = {"topic": body.topic.strip()}
    s.add(ch)
    s.commit()
    s.refresh(ch)
    routes = body.routes if body.routes is not None else [
        RouteIn(types=nt.DEFAULT_CHAT_TYPES if ch.kind in CHAT_KINDS else [], min_severity="info")]
    _set_routes(s, ch.id, routes)
    s.commit()
    return _out(s, ch)


@router.put("/channels/{channel_id}")
async def update_channel(channel_id: str, body: ChannelIn, s: Session = Depends(get_session)):
    ch = s.get(NotificationChannel, channel_id)
    if ch is None:
        raise HTTPException(404)
    _check(s, body, ch)
    ch.name, ch.enabled = body.name.strip(), body.enabled
    ch.decisions_allowed = body.decisions_allowed and ch.kind in CHAT_KINDS
    if body.default_agent_id != ch.default_agent_id:
        ch.default_agent_id, ch.chat_thread_id = body.default_agent_id or None, None
    if ch.kind == "telegram" and body.token.strip():
        trial = NotificationChannel(**{**ch.model_dump(), "config": {"token": body.token.strip()}})
        found = await _verify(trial)
        ch.config = {**(ch.config or {}), **found, "token": seal(body.token.strip())}
    elif ch.kind == "mqtt":
        ch.config = {**(ch.config or {}), "topic": body.topic.strip()}
    s.add(ch)
    if body.routes is not None:
        _set_routes(s, ch.id, body.routes)
    s.commit()
    s.refresh(ch)
    return _out(s, ch)


@router.delete("/channels/{channel_id}", status_code=204)
def delete_channel(channel_id: str, s: Session = Depends(get_session)):
    ch = s.get(NotificationChannel, channel_id)
    if ch is None:
        raise HTTPException(404)
    for r in _routes(s, channel_id):
        s.delete(r)
    s.delete(ch)
    s.commit()


@router.post("/channels/{channel_id}/pair")
def pair(channel_id: str, s: Session = Depends(get_session)):
    """A one-time code (10 minutes). Send /start <code> to the bot, or open the link."""
    ch = s.get(NotificationChannel, channel_id)
    if ch is None:
        raise HTTPException(404)
    if ch.kind not in CHAT_KINDS:
        raise HTTPException(422, "only chat channels are paired")
    code, until = nt.new_pairing_code(channel_id)
    bot = (ch.config or {}).get("bot_username", "")
    return {"code": code, "expires_at": until, "command": f"/start {code}",
            "link": f"https://t.me/{bot}?start={code}" if ch.kind == "telegram" and bot else ""}


@router.delete("/channels/{channel_id}/paired/{user_id}", status_code=204)
def unpair(channel_id: str, user_id: str, s: Session = Depends(get_session)):
    ch = s.get(NotificationChannel, channel_id)
    if ch is None:
        raise HTTPException(404)
    ch.paired = [p for p in ch.paired or [] if str(p.get("user_id")) != user_id]
    s.add(ch)
    s.commit()


@router.post("/channels/{channel_id}/test")
async def test_channel(channel_id: str, s: Session = Depends(get_session)):
    """Send a test notice to this channel only."""
    ch = s.get(NotificationChannel, channel_id)
    if ch is None:
        raise HTTPException(404)
    if ch.kind in CHAT_KINDS and not nt.destinations(ch):
        raise HTTPException(409, "nobody is paired yet: pair your account first")
    notice = Notice(type="test", title="Test from Agent HQ",
                    body="Notifications reach you here. Reply to a notice to answer it.",
                    link=nt.link("/settings/notifications"),
                    event={"type": "test", "at": datetime.now(timezone.utc).isoformat()})
    s.add(notice)
    s.commit()
    s.refresh(notice)
    ok = await nt.deliver(notice, ch)
    s.expire_all()
    fresh = s.get(NotificationChannel, channel_id)
    if not ok:
        raise HTTPException(502, f"not sent: {fresh.last_error or 'unknown error'}")
    return {"sent": True}


@router.get("/channels/{channel_id}/log")
def channel_log(channel_id: str, limit: int = 50, s: Session = Depends(get_session)):
    """Recent deliveries and what came in, newest first."""
    limit = max(1, min(limit, 200))
    dels = s.exec(select(NoticeDelivery).where(NoticeDelivery.channel_id == channel_id)
                  .order_by(col(NoticeDelivery.at).desc()).limit(limit)).all()
    notices = {n.id: n for n in s.exec(select(Notice).where(
        col(Notice.id).in_([d.notice_id for d in dels]))).all()} if dels else {}
    ins = s.exec(select(ChannelInbound).where(ChannelInbound.channel_id == channel_id)
                 .order_by(col(ChannelInbound.at).desc()).limit(limit)).all()
    rows = [{"at": d.at, "direction": "out", "kind": notices[d.notice_id].type if d.notice_id in notices else "",
             "status": d.status, "text": notices[d.notice_id].title if d.notice_id in notices else "",
             "outcome": notices[d.notice_id].outcome if d.notice_id in notices else "", "error": d.error}
            for d in dels]
    rows += [{"at": i.at, "direction": "in", "kind": i.kind, "status": i.kind, "text": i.detail,
              "sender": i.sender_name or i.sender_id, "thread_id": i.thread_id} for i in ins]
    rows.sort(key=lambda r: nt._aware(r["at"]), reverse=True)
    return rows[:limit]
