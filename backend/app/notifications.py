"""Notifications: requests and alerts for you, on MQTT, Telegram and (later) Discord.

Everything that wants your attention calls subagents.notify(event) with a small dict, as it
always has. Here that event becomes a Notice (a title, a body, a link and the decisions it
allows) and goes to every channel whose rules match its type and agent (Settings →
Notifications). The drivers in app/channels/ only move messages.

Chat channels also take your answers, from paired people only:
* a button decides the request with the same code the web UI calls (decide_approval,
  decide_request, ...), and a reply that starts with approve/deny does too, with the rest
  as your note;
* any other reply to a notice goes to that notice's conversation as your message;
* a message that isn't a reply goes to the channel's default agent, in the channel's chat.
The agent's answer to a turn you started from a chat goes back to that chat.

Deciding settles a request everywhere: whichever way you decide it (web UI, Telegram, ...),
every message sent about it is edited to show the outcome, and its buttons go away.
"""

from __future__ import annotations

import asyncio
import contextvars
import hmac
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic_ai import FunctionToolset, RunContext
from sqlmodel import select

from . import db
from .channels import CHAT_KINDS, ChannelError, driver_for
from .config import settings
from .models import (
    AgentDef,
    ChannelInbound,
    Notice,
    NoticeDelivery,
    NotificationChannel,
    NotificationRoute,
    SystemSetting,
    Thread,
    ToolApproval,
    WorkItem,
)

log = logging.getLogger("agent_hq.notifications")

# --------------------------------------------------------------------------- catalogue

# Every notice type, for the routing rules in the UI. "request": something to decide or
# answer; "info": worth knowing; "decision": the echo of a decision (mostly for MQTT).
TYPES: dict[str, dict[str, str]] = {
    "tool_approval": {"label": "Tool approvals", "group": "request"},
    "subagent_request": {"label": "Sub-agent requests", "group": "request"},
    "skill_proposal": {"label": "Skill proposals", "group": "request"},
    "profile_proposal": {"label": "Profile suggestions", "group": "request"},
    "knowledge_proposal": {"label": "Knowledge suggestions", "group": "request"},
    "goal_review": {"label": "Goals to confirm done", "group": "request"},
    "goal_proposal": {"label": "Goal proposals", "group": "request"},
    "goal_question": {"label": "Goal questions", "group": "request"},
    "desktop_request": {"label": "Desktop take-over requests", "group": "request"},
    "agent_alert": {"label": "Agent alerts", "group": "alert"},
    "goal_blocked": {"label": "Blocked goals", "group": "info"},
    "skill_applied": {"label": "Skills applied (auto learning)", "group": "info"},
    "tool_approval_decision": {"label": "Tool approval decisions", "group": "decision"},
    "subagent_decision": {"label": "Sub-agent decisions", "group": "decision"},
}
# What a new Telegram or Discord channel gets: everything but the decision echoes.
DEFAULT_CHAT_TYPES = [t for t, v in TYPES.items() if v["group"] != "decision"]
SEVERITIES = ("info", "warning", "critical")
SEVERITY_ICON = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}

DEFAULTS: dict[str, Any] = {
    "alerts_per_hour": 10,   # send_alert calls per agent per hour
    "linger_minutes": 30,    # after you answer from a chat, later reports in that conversation go there too
}

APPROVE = {"approve", "done", "ready", "backlog"}
APPROVE_WORDS = {"approve", "approved", "yes", "y", "ok", "okay", "allow", "go", "confirm", "done"}
DENY_WORDS = {"deny", "denied", "no", "n", "reject", "stop", "refuse", "back"}
PAIR_MINUTES = 10

# Who is deciding, for the outcome line on every copy ("via Telegram (Mike)").
_via: contextvars.ContextVar[str] = contextvars.ContextVar("notice_via", default="in Agent HQ")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def get_settings() -> dict[str, Any]:
    with db.session() as s:
        row = s.get(SystemSetting, "notifications")
    return {**DEFAULTS, **(row.value if row else {})}


def save_settings(values: dict[str, Any]) -> dict[str, Any]:
    merged = {**get_settings(), **{k: v for k, v in values.items() if k in DEFAULTS}}
    with db.session() as s:
        row = s.get(SystemSetting, "notifications") or SystemSetting(key="notifications")
        row.value = merged
        s.add(row)
        s.commit()
    return merged


def link(path: str) -> str:
    return f"{settings.public_url}{path}" if settings.public_url else ""


# --------------------------------------------------------------------------- the default channel

def ensure_defaults() -> None:
    """Once: an MQTT channel standing in for the old built-in MQTT notifications, on if
    AHQ_MQTT_URL is set and they weren't turned off (Settings → Sub-agents used to have the
    switch). Deleting it afterwards sticks."""
    with db.session() as s:
        if s.get(SystemSetting, "notifications_seeded") is not None:
            return
        old = s.get(SystemSetting, "subagents")
        on = bool(settings.mqtt_url) and bool((old.value if old else {}).get("notify_mqtt", True))
        if s.exec(select(NotificationChannel).where(NotificationChannel.kind == "mqtt")).first() is None:
            ch = NotificationChannel(name="MQTT", kind="mqtt", enabled=on, decisions_allowed=False,
                                     config={"topic": ""})
            s.add(ch)
            s.add(NotificationRoute(channel_id=ch.id, types=[], agent_ids=[]))
        s.add(SystemSetting(key="notifications_seeded", value={"at": _now().isoformat()}))
        try:
            s.commit()
        except Exception:  # another process got there first
            s.rollback()


_seeded_for: int | None = None


def _ensure_once() -> None:
    """ensure_defaults, once per database this process uses."""
    global _seeded_for
    if _seeded_for != id(db.engine()):
        ensure_defaults()
        _seeded_for = id(db.engine())


# --------------------------------------------------------------------------- building a notice

def _goal(goal_id: str | None) -> WorkItem | None:
    if not goal_id:
        return None
    with db.session() as s:
        return s.get(WorkItem, goal_id)


def _cut(text: Any, n: int) -> str:
    text = "" if text is None else str(text)
    return text if len(text) <= n else text[:n].rstrip() + "…"


def describe(event: dict[str, Any]) -> Notice:
    """Turn an event (what subagents.notify is given) into a notice."""
    t = event.get("type", "")
    agent = event.get("agent") or ""
    n = Notice(type=t, agent_name=agent, link=event.get("link") or "", thread_id=event.get("thread_id"),
               event=json.loads(json.dumps(event, default=str)))
    approve_deny = [{"id": "approve", "label": "✅ Approve"}, {"id": "deny", "label": "❌ Deny"}]
    if t == "tool_approval":
        n.ref_kind, n.ref_id = t, event.get("approval_id", "")
        n.title = f"{agent} wants to run {event.get('tool')} on {event.get('server')}"
        args = event.get("args")
        if args is None and n.ref_id:
            with db.session() as s:
                row = s.get(ToolApproval, n.ref_id)
            args = (row.args if row else {}) or {}
        lines = [f"Why: {event['reason']}"] if event.get("reason") else []
        lines += [f"{k}: {_cut(v, 600)}" for k, v in (args or {}).items()]
        n.body = _cut("\n".join(lines), 2500)
        n.actions = approve_deny
    elif t == "subagent_request":
        n.ref_kind, n.ref_id = t, event.get("request_id", "")
        subs = event.get("subagents") or []
        n.title = f"{agent} asks for {len(subs)} sub-agent{'s' if len(subs) != 1 else ''}"
        lines = [_cut(event.get("why"), 800)]
        lines += [f"• {r.get('role')}: {_cut(r.get('purpose'), 160)} ({r.get('label') or 'own model'}, "
                  f"~{r.get('est_minutes')} min)" for r in subs]
        lines.append(f"About {event.get('total_minutes')} model-minutes; grant for {event.get('hours')} h. "
                     "To change the terms, decide it in Agent HQ.")
        n.body = "\n".join(x for x in lines if x)
        n.actions = [{"id": "approve", "label": "✅ Approve as asked"}, {"id": "deny", "label": "❌ Deny"}]
    elif t == "skill_proposal":
        n.ref_kind, n.ref_id = t, event.get("proposal_id", "")
        what = "a change to the skill" if event.get("revision") else "a new skill"
        n.title = f"{agent} proposes {what}: {event.get('title') or event.get('skill')}"
        n.body = _cut(event.get("reason"), 1500)
        n.actions = [{"id": "approve", "label": "✅ Approve"}, {"id": "deny", "label": "❌ Reject"}]
    elif t == "profile_proposal":
        n.ref_kind, n.ref_id = t, event.get("proposal_id", "")
        n.title = f"{agent} suggests adding to your profile"
        n.body = _cut(event.get("text"), 1500)
        n.actions = [{"id": "approve", "label": "✅ Add it"}, {"id": "deny", "label": "❌ Reject"}]
    elif t == "knowledge_proposal":
        n.ref_kind, n.ref_id = t, event.get("proposal_id", "")
        n.title = f"{agent} suggests adding “{event.get('title')}” to {event.get('collection')}"
        n.body = "Read it in Agent HQ before approving, or reject it here."
        n.actions = [{"id": "approve", "label": "✅ Add it"}, {"id": "deny", "label": "❌ Reject"}]
    elif t in ("goal_review", "goal_proposal", "goal_question", "goal_blocked"):
        g = _goal(event.get("goal_id"))
        n.thread_id = n.thread_id or (g.thread_id if g else None)
        title = event.get("title") or (g.title if g else "")
        n.ref_kind, n.ref_id = t, event.get("goal_id", "")
        if t == "goal_review":
            n.title = f"{agent} says this goal is done: {title}"
            n.body = _cut(event.get("evidence"), 2500)
            n.actions = [{"id": "done", "label": "✅ Confirm done"}, {"id": "deny", "label": "↩️ Send back"}]
        elif t == "goal_proposal":
            n.title = f"{event.get('proposed_by') or agent} proposes a goal: {title}"
            n.body = _cut(event.get("reason"), 2000)
            n.actions = [{"id": "ready", "label": "✅ To Ready"}, {"id": "backlog", "label": "📥 To Backlog"},
                         {"id": "deny", "label": "❌ Reject"}]
        elif t == "goal_question":
            n.title = f"{agent} needs you: {title}"
            n.body = _cut(event.get("question"), 2500) + "\n\nReply to this message to answer."
        else:
            n.title = f"Goal blocked: {title}"
            n.body = _cut(event.get("reason"), 1500)
    elif t == "desktop_request":
        n.title = f"{agent} needs you on its desktop"
        n.body = _cut(event.get("reason"), 1500) + "\n\nTake over in Agent HQ, then hand it back."
    elif t == "agent_alert":
        sev = event.get("severity") if event.get("severity") in SEVERITIES else "warning"
        n.severity = sev
        n.title = f"{SEVERITY_ICON[sev]} {agent}"
        n.body = _cut(event.get("text"), 3500)
    elif t == "skill_applied":
        n.title = f"{agent} added the skill {event.get('skill')} (v{event.get('version')})"
    elif t == "tool_approval_decision":
        n.title = f"{event.get('tool')} for {agent}: {event.get('status')}"
        n.body = _cut(event.get("note"), 500)
    elif t == "subagent_decision":
        n.title = f"{agent}'s sub-agent request: {event.get('status')}"
        n.body = _cut(event.get("note"), 500)
    else:
        n.title = t.replace("_", " ").capitalize() + (f" ({agent})" if agent else "")
    if not n.link and n.thread_id:
        n.link = link(f"/chat/{n.thread_id}")
    return n


# --------------------------------------------------------------------------- routing

def _rank(sev: str) -> int:
    return SEVERITIES.index(sev) if sev in SEVERITIES else 1


def matches(route: NotificationRoute, notice: Notice, agent_names: dict[str, str]) -> bool:
    if not route.enabled:
        return False
    if route.types and notice.type not in route.types:
        return False
    if route.agent_ids:
        names = {agent_names.get(a) for a in route.agent_ids}
        if notice.agent_name not in names:
            return False
    if notice.type == "agent_alert" and _rank(notice.severity) < _rank(route.min_severity):
        return False
    return True


def channels_for(notice: Notice) -> list[NotificationChannel]:
    with db.session() as s:
        chans = s.exec(select(NotificationChannel).where(NotificationChannel.enabled == True)).all()  # noqa: E712
        routes = s.exec(select(NotificationRoute)).all()
        names = {a.id: a.name for a in s.exec(select(AgentDef)).all()}
    by_channel: dict[str, list[NotificationRoute]] = {}
    for r in routes:
        by_channel.setdefault(r.channel_id, []).append(r)
    return [c for c in chans if any(matches(r, notice, names) for r in by_channel.get(c.id, []))]


def destinations(ch: NotificationChannel) -> list[str]:
    """Where a channel's notices go: each paired chat, once (MQTT: its one topic)."""
    if ch.kind not in CHAT_KINDS:
        return [""]
    seen: list[str] = []
    for p in ch.paired or []:
        c = str(p.get("chat_id") or "")
        if c and c not in seen:
            seen.append(c)
    return seen


async def dispatch(event: dict[str, Any]) -> bool:
    """Store the notice and send it to every channel whose rules match. Best effort: a
    channel that fails is noted on the channel and in the delivery log. True if it went
    somewhere."""
    _ensure_once()
    notice = describe({**event, "at": event.get("at") or _now().isoformat()})
    targets = channels_for(notice)
    if not targets:
        return False
    with db.session() as s:
        s.add(notice)
        s.commit()
        s.refresh(notice)
    results = await asyncio.gather(*(deliver(notice, ch) for ch in targets), return_exceptions=True)
    return any(r is True for r in results)


async def deliver(notice: Notice, ch: NotificationChannel, chat_ids: list[str] | None = None) -> bool:
    try:
        drv = driver_for(ch)
    except ChannelError as e:
        _record(notice, ch, "", "", str(e))
        return False
    ok = False
    for chat_id in (destinations(ch) if chat_ids is None else chat_ids):
        try:
            async with asyncio.timeout(30):
                mid = await drv.send(notice, chat_id, decisions=ch.decisions_allowed)
            _record(notice, ch, chat_id, mid or "")
            ok = True
        except Exception as e:
            log.warning("notice %s to channel %s failed: %s", notice.type, ch.name, e)
            _record(notice, ch, chat_id, "", str(e) or type(e).__name__)
    return ok


def _record(notice: Notice, ch: NotificationChannel, chat_id: str, message_id: str, error: str = "") -> None:
    with db.session() as s:
        s.add(NoticeDelivery(notice_id=notice.id, channel_id=ch.id, chat_id=str(chat_id),
                             message_id=str(message_id), status="failed" if error else "sent",
                             error=error[:500]))
        row = s.get(NotificationChannel, ch.id)
        if row is not None:
            if error:
                row.last_error = error[:500]
            else:
                row.last_error, row.last_ok_at = "", _now()
            s.add(row)
        s.commit()


# --------------------------------------------------------------------------- settling

OUTCOME = {"approved": "✅ Approved", "applied": "✅ Approved", "denied": "❌ Denied",
           "rejected": "❌ Rejected", "done": "✅ Confirmed done", "sent_back": "↩️ Sent back",
           "ready": "✅ Approved to Ready", "backlog": "📥 Approved to Backlog",
           "dropped": "❌ Rejected", "expired": "⌛ Expired", "gone": "No longer pending"}


async def settled(ref_kind: str, ref_id: str, status: str) -> None:
    """A request was decided (anywhere): edit every message about it. Never raises."""
    try:
        outcome = f"{OUTCOME.get(status, status.capitalize())} {_via.get()}".strip()
        with db.session() as s:
            notices = s.exec(select(Notice).where(Notice.ref_kind == ref_kind, Notice.ref_id == ref_id,
                                                  Notice.status == "open")).all()
            for n in notices:
                n.status, n.outcome, n.settled_at = "settled", outcome, _now()
                s.add(n)
            s.commit()
            for n in notices:
                s.refresh(n)
            ids = [n.id for n in notices]
            sent = s.exec(select(NoticeDelivery).where(NoticeDelivery.notice_id.in_(ids),
                                                       NoticeDelivery.message_id != "")).all() if ids else []
            chans = {c.id: c for c in s.exec(select(NotificationChannel)).all()}
        by_id = {n.id: n for n in notices}
        jobs = []
        for d in sent:
            ch = chans.get(d.channel_id)
            if ch is None or not ch.enabled:
                continue
            jobs.append(_settle_one(ch, by_id[d.notice_id], d))
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
    except Exception:
        log.warning("couldn't settle %s %s", ref_kind, ref_id, exc_info=True)


async def _settle_one(ch: NotificationChannel, n: Notice, d: NoticeDelivery) -> None:
    try:
        async with asyncio.timeout(20):
            await driver_for(ch).settle(n, d.chat_id, d.message_id)
    except Exception as e:
        log.info("couldn't edit notice %s on %s: %s", n.id, ch.name, e)


# --------------------------------------------------------------------------- inbound

@dataclass
class Sender:
    user_id: str
    chat_id: str
    name: str = ""


@dataclass
class Reply:
    text: str = ""
    ack: bool = False


def _channel(channel_id: str) -> NotificationChannel | None:
    with db.session() as s:
        return s.get(NotificationChannel, channel_id)


def _log_in(channel_id: str, kind: str, sender: Sender, detail: str = "", *,
            notice_id: str | None = None, thread_id: str | None = None) -> None:
    with db.session() as s:
        s.add(ChannelInbound(channel_id=channel_id, kind=kind, sender_id=sender.user_id[:64],
                             sender_name=sender.name[:120], notice_id=notice_id, thread_id=thread_id,
                             detail=detail[:1000]))
        s.commit()
    log.info("channel %s: %s from %s (%s) %s", channel_id, kind, sender.name, sender.user_id, detail[:120])


def is_paired(ch: NotificationChannel, sender: Sender) -> bool:
    return any(str(p.get("user_id")) == sender.user_id for p in ch.paired or [])


def new_pairing_code(channel_id: str) -> tuple[str, datetime]:
    code = secrets.token_hex(4).upper()
    until = _now() + timedelta(minutes=PAIR_MINUTES)
    with db.session() as s:
        ch = s.get(NotificationChannel, channel_id)
        if ch is None:
            raise LookupError("no such channel")
        ch.pairing_code, ch.pairing_expires_at = code, until
        s.add(ch)
        s.commit()
    return code, until


def _try_pair(ch: NotificationChannel, sender: Sender, code: str) -> bool:
    if not (ch.pairing_code and code):
        return False
    if (_aware(ch.pairing_expires_at) or _now()) < _now():
        return False
    if not hmac.compare_digest(ch.pairing_code.upper(), code.strip().upper()):
        return False
    with db.session() as s:
        row = s.get(NotificationChannel, ch.id)
        paired = [p for p in (row.paired or []) if str(p.get("user_id")) != sender.user_id]
        paired.append({"user_id": sender.user_id, "chat_id": sender.chat_id, "name": sender.name,
                       "paired_at": _now().isoformat()})
        row.paired, row.pairing_code, row.pairing_expires_at = paired, None, None
        s.add(row)
        s.commit()
    return True


def _delivery(channel_id: str, chat_id: str, message_id: str) -> tuple[NoticeDelivery | None, Notice | None]:
    with db.session() as s:
        d = s.exec(select(NoticeDelivery).where(NoticeDelivery.channel_id == channel_id,
                                                NoticeDelivery.chat_id == chat_id,
                                                NoticeDelivery.message_id == message_id)).first()
        n = s.get(Notice, d.notice_id) if d else None
    return d, n


def _word(text: str) -> tuple[str, str]:
    """('approve'|'deny'|'', the rest as a note) from a reply like 'no, use pve2'."""
    head, _, rest = text.strip().partition(" ")
    w = head.strip(".,:;!-—").lower()
    if w in APPROVE_WORDS:
        return "approve", rest.strip(" ,:;-—")
    if w in DENY_WORDS:
        return "deny", rest.strip(" ,:;-—")
    return "", text


class Inbox:
    """What chat channels hand their incoming updates to. Lives in the API process."""

    def __init__(self, rt) -> None:
        self.rt = rt

    # -- bookkeeping the drivers use
    async def get_offset(self, channel_id: str) -> int | None:
        v = await self.rt.broker.r.get(f"ahq:channel:{channel_id}:offset")
        return int(v) if v else None

    async def set_offset(self, channel_id: str, offset: int) -> None:
        await self.rt.broker.r.set(f"ahq:channel:{channel_id}:offset", offset)

    async def channel_ok(self, channel_id: str) -> None:
        with db.session() as s:
            row = s.get(NotificationChannel, channel_id)
            if row is not None and (row.last_error or not row.last_ok_at
                                    or _now() - _aware(row.last_ok_at) > timedelta(minutes=5)):
                row.last_error, row.last_ok_at = "", _now()
                s.add(row)
                s.commit()

    async def channel_error(self, channel_id: str, error: str) -> None:
        log.warning("channel %s: %s", channel_id, error)
        with db.session() as s:
            row = s.get(NotificationChannel, channel_id)
            if row is not None and row.last_error != error[:500]:
                row.last_error = error[:500]
                s.add(row)
                s.commit()

    # -- buttons
    async def on_action(self, channel_id: str, sender: Sender, notice_id: str, action: str) -> str:
        ch = _channel(channel_id)
        if ch is None or not ch.enabled:
            return "This channel is off."
        if not is_paired(ch, sender):
            _log_in(channel_id, "refused", sender, f"button {action} on {notice_id}", notice_id=notice_id)
            return "You're not paired with this Agent HQ."
        with db.session() as s:
            n = s.get(Notice, notice_id)
        if n is None:
            return "That notice is gone."
        return await self._decide(ch, sender, n, action, "")

    async def _decide(self, ch: NotificationChannel, sender: Sender, n: Notice, action: str, note: str) -> str:
        if not ch.decisions_allowed:
            _log_in(ch.id, "refused", sender, f"{action} on {n.type} (decisions are off)", notice_id=n.id)
            return "Decisions are turned off for this channel: decide it in Agent HQ."
        if n.status != "open":
            return n.outcome or "Already decided."
        if action not in {a["id"] for a in n.actions}:
            return "That isn't a choice for this request."
        if n.thread_id:  # before deciding: the turn it wakes may answer at once
            await self.linger(n.thread_id, ch.id, sender.chat_id)
        via = f"via {ch.name}" + (f" ({sender.name})" if sender.name else "")
        token = _via.set(via)
        try:
            result = await self._apply(n, action, note)
        except LookupError:
            await settled(n.ref_kind, n.ref_id, "gone")
            result = "That request no longer exists."
        except ValueError as e:  # already decided, expired, not pending (WorkError is one too)
            await settled(n.ref_kind, n.ref_id, "gone")
            result = f"Not done: {e}"
        finally:
            _via.reset(token)
        _log_in(ch.id, "decision", sender, f"{n.type} {n.ref_id}: {action}"
                + (f" — {note}" if note else "") + f" → {result}", notice_id=n.id, thread_id=n.thread_id)
        return result

    async def _apply(self, n: Notice, action: str, note: str) -> str:
        rt, ok, kind = self.rt, action in APPROVE, n.ref_kind
        if kind == "tool_approval":
            r = await rt.decide_approval(n.ref_id, ok, note=note)
            return f"{r.status.capitalize()}."
        if kind == "subagent_request":
            r = await rt.decide_request(n.ref_id, ok, note=note)
            return f"{r.status.capitalize()}."
        if kind == "skill_proposal":
            r = await rt.decide_skill_proposal(n.ref_id, ok, note=note, by=_via.get())
            return f"{r.status.capitalize()}."
        if kind == "profile_proposal":
            r = await rt.decide_profile_proposal(n.ref_id, ok, note=note)
            return f"{r.status.capitalize()}."
        if kind == "knowledge_proposal":
            r = await rt.decide_knowledge_proposal(n.ref_id, ok, note=note)
            return f"{r.status.capitalize()}."
        if kind == "goal_review":
            from . import workitems

            await workitems.review_and_record(rt, n.ref_id, ok, note)
            return "Confirmed done." if ok else "Sent back."
        if kind == "goal_proposal":
            from . import workitems

            await workitems.proposal_and_record(rt, n.ref_id, ok, ready=action == "ready", note=note)
            return {"ready": "Approved to Ready.", "backlog": "Approved to Backlog."}.get(action, "Rejected.")
        raise LookupError("nothing to decide")

    # -- messages
    async def on_message(self, channel_id: str, sender: Sender, text: str, *, reply_to: str | None,
                         message_id: str, has_files: bool = False) -> Reply | None:
        ch = _channel(channel_id)
        if ch is None or not ch.enabled:
            return None
        text = (text or "").strip()
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@", 1)[0].lower() if cmd.startswith("/") else ""
        if not is_paired(ch, sender):
            if cmd in ("/start", "/pair") and _try_pair(ch, sender, arg):
                _log_in(channel_id, "pairing", sender, f"paired chat {sender.chat_id}")
                return Reply("Paired with Agent HQ. Requests and alerts will come here; reply to one "
                             "to answer it, or just write to talk to "
                             + (_agent_name(ch.default_agent_id) or "your agents") + ".")
            _log_in(channel_id, "refused", sender, _cut(text, 200))
            return None  # strangers get nothing back: no hint that anything is here
        if cmd in ("/start", "/pair"):
            return Reply("You're already paired.")
        if cmd == "/help":
            return Reply(HELP)
        if cmd == "/new":
            if _agent_name(ch.default_agent_id) is None:
                return Reply("No default agent is set for this channel (Settings → Notifications).")
            self.new_chat(ch)
            return Reply(f"New conversation with {_agent_name(ch.default_agent_id)}.")
        if has_files and not text:
            return Reply("Only text for now: send files in Agent HQ.")
        if not text:
            return None
        if reply_to:
            _, n = _delivery(channel_id, sender.chat_id, reply_to)
            if n is not None:
                return await self._reply_to_notice(ch, sender, n, text, message_id)
        if not ch.default_agent_id or _agent_name(ch.default_agent_id) is None:
            return Reply("Reply to a notice to answer it. To chat with an agent from here, set a "
                         "default agent for this channel in Settings → Notifications.")
        tid = ch.chat_thread_id if _thread_live(ch.chat_thread_id) else self.new_chat(ch)
        await self._post(ch, sender, tid, text, message_id)
        _log_in(channel_id, "chat", sender, _cut(text, 300), thread_id=tid)
        return Reply(ack=True)

    async def _reply_to_notice(self, ch: NotificationChannel, sender: Sender, n: Notice, text: str,
                               message_id: str) -> Reply:
        if n.actions and n.status == "open":
            word, note = _word(text)
            if not word:
                return Reply("Use the buttons, or reply starting with approve or deny (a note can follow).")
            action = "deny" if word == "deny" else next(
                (a["id"] for a in n.actions if a["id"] in APPROVE), "approve")
            return Reply(await self._decide(ch, sender, n, action, note))
        if not n.thread_id or not _thread_live(n.thread_id):
            return Reply("That conversation is gone. Write without replying to talk to "
                         + (_agent_name(ch.default_agent_id) or "an agent") + ".")
        await self._post(ch, sender, n.thread_id, text, message_id)
        _log_in(ch.id, "reply", sender, _cut(text, 300), notice_id=n.id, thread_id=n.thread_id)
        return Reply(ack=True)

    async def _post(self, ch: NotificationChannel, sender: Sender, thread_id: str, text: str,
                    message_id: str) -> None:
        await self.rt.enqueue_turn(thread_id, text, channel_id=ch.id, chat_id=sender.chat_id,
                                   channel_message_id=message_id, via=ch.name)
        await self.linger(thread_id, ch.id, sender.chat_id)

    def new_chat(self, ch: NotificationChannel) -> str:
        name = _agent_name(ch.default_agent_id) or "agent"
        with db.session() as s:
            t = Thread(agent_id=ch.default_agent_id, title=f"{ch.name}: {name}")
            s.add(t)
            s.commit()
            s.refresh(t)
            row = s.get(NotificationChannel, ch.id)
            row.chat_thread_id = t.id
            s.add(row)
            s.commit()
            return t.id

    async def linger(self, thread_id: str, channel_id: str, chat_id: str) -> None:
        minutes = int(get_settings().get("linger_minutes") or 0)
        if minutes > 0:
            await self.rt.broker.r.set(f"ahq:channel:linger:{thread_id}",
                                       json.dumps({"channel_id": channel_id, "chat_id": chat_id}),
                                       ex=minutes * 60)


HELP = ("Agent HQ\n"
        "• Buttons decide requests. Or reply to a request with approve or deny, and a note after it.\n"
        "• Reply to any other notice or agent message to continue that conversation.\n"
        "• Write without replying to talk to this channel's default agent.\n"
        "• /new starts a new conversation with it (!new on Discord).")


def _agent_name(agent_id: str | None) -> str | None:
    if not agent_id:
        return None
    with db.session() as s:
        a = s.get(AgentDef, agent_id)
    return a.name if a and a.enabled else None


def _thread_live(thread_id: str | None) -> bool:
    if not thread_id:
        return False
    with db.session() as s:
        t = s.get(Thread, thread_id)
    return t is not None and t.deleted_at is None


# --------------------------------------------------------------------------- replies back

# Turns whose replies don't follow you to a chat even while a conversation lingers there:
# they start on their own and would turn a chat into a feed.
NOT_LINGERING = {"goal", "mqtt", "webhook", "schedule"}


async def reply_back(rt, thread_id: str, job, text: str, agent_name: str) -> None:
    """After a turn: send the agent's reply to the chat it should go to, if any. Never raises.

    * a turn you started from a chat: back to that chat;
    * any turn in a channel's own chat (its default agent's conversation): to its chats;
    * a background report or decision follow-up shortly after you answered from a chat
      (linger_minutes): to that chat."""
    try:
        meta = job.meta or {}
        targets: dict[tuple[str, str], str | None] = {}
        if meta.get("channel_id"):
            targets[(meta["channel_id"], str(meta.get("chat_id") or ""))] = meta.get("channel_message_id")
        else:
            with db.session() as s:
                own = s.exec(select(NotificationChannel).where(
                    NotificationChannel.chat_thread_id == thread_id)).all()
            for ch in own:
                for c in destinations(ch):
                    targets[(ch.id, c)] = None
            if not targets and meta.get("kind") not in NOT_LINGERING:
                raw = await rt.broker.r.get(f"ahq:channel:linger:{thread_id}")
                if raw:
                    v = json.loads(raw)
                    targets[(v["channel_id"], str(v.get("chat_id") or ""))] = None
        if not targets:
            return
        notice = Notice(type="agent_reply", thread_id=thread_id, agent_name=agent_name, title=agent_name,
                        body=text or "(no reply)", link=link(f"/chat/{thread_id}"))
        with db.session() as s:
            s.add(notice)
            s.commit()
            s.refresh(notice)
        for (channel_id, chat_id), _reply_to in targets.items():
            ch = _channel(channel_id)
            if ch is None or not ch.enabled or not chat_id:
                continue
            await deliver(notice, ch, [chat_id])
    except Exception:
        log.warning("couldn't send the reply in %s back to its chat", thread_id, exc_info=True)


# --------------------------------------------------------------------------- alerts (agent tool)

ALERT_RULES = (
    "## Alerts\n"
    "send_alert pings the user on their phone (Telegram, Discord or MQTT, whatever they set up). "
    "Use it only for something they should know about now: an outage, a failure that needs them, "
    "a finding they asked to be told about. Not for routine results, progress or anything you're "
    "reporting in this conversation anyway. Put the essentials in the message itself (what, where, "
    "how bad, what you recommend); they may read only that. They can reply to it, and the reply "
    "comes to this conversation.")


def alert_toolset(rt):
    from .models import Role

    async def send_alert(ctx: RunContext, message: str, severity: str = "warning") -> dict:
        """Alert the user on their phone now. They can reply, and the reply comes here.

        Args:
            message: What happened, where, how bad, and what you recommend, in a few lines.
            severity: info, warning or critical.
        """
        from .runtime import load_agent

        me = load_agent(ctx.deps.agent_id)
        message = (message or "").strip()
        if not message:
            return {"error": "say what the alert is about in message"}
        severity = severity if severity in SEVERITIES else "warning"
        per_hour = int(get_settings().get("alerts_per_hour") or 0)
        key = f"ahq:alerts:{me.id}:{_now():%Y%m%d%H}"
        n = await rt.broker.r.incr(key)
        await rt.broker.r.expire(key, 3700)
        if per_hour and n > per_hour:
            return {"status": "refused", "summary": f"Not sent: you've sent {per_hour} alerts this hour, "
                    "the most allowed. Report in this conversation instead."}
        thread_id = ctx.deps.thread_id
        await rt.save_message(thread_id, Role.event, f"{me.name} sent you an alert ({severity}): {message}",
                              agent_id=me.id, kind="agent_alert", severity=severity)
        from . import subagents

        sent = await subagents.notify({"type": "agent_alert", "agent": me.name, "severity": severity,
                                       "text": message, "thread_id": thread_id,
                                       "link": link(f"/chat/{thread_id}") if thread_id else ""})
        if not sent:
            return {"status": "not_delivered", "summary": "No notification channel takes this alert "
                    "(the user hasn't routed alerts anywhere). Report it in your reply."}
        return {"status": "sent", "summary": "The user was alerted. Mention it in one line."}

    return FunctionToolset([send_alert], id="alerts")


# --------------------------------------------------------------------------- listeners

KEEP_DAYS = 30


def prune(days: int = KEEP_DAYS) -> int:
    """Forget notices, deliveries and inbound log rows older than `days`. Replies to a
    message that old start a chat with the default agent instead."""
    from sqlalchemy import delete

    cutoff = _now() - timedelta(days=days)
    with db.session() as s:
        old = list(s.exec(select(Notice.id).where(Notice.created_at < cutoff)).all())
        for i in range(0, len(old), 500):
            part = old[i:i + 500]
            s.exec(delete(NoticeDelivery).where(NoticeDelivery.notice_id.in_(part)))
            s.exec(delete(Notice).where(Notice.id.in_(part)))
        s.exec(delete(ChannelInbound).where(ChannelInbound.at < cutoff))
        s.commit()
    return len(old)

class Listeners:
    """Runs one listener per enabled chat channel, in the API process. A Valkey lock per
    channel keeps two API processes from both reading one bot (Telegram allows one reader)."""

    def __init__(self, rt, refresh: float = 10) -> None:
        self.rt = rt
        self.inbox = Inbox(rt)
        self.refresh = refresh
        self.tasks: dict[str, tuple[str, asyncio.Task]] = {}

    @staticmethod
    def _fingerprint(ch: NotificationChannel) -> str:
        return json.dumps([ch.kind, ch.config], sort_keys=True, default=str)

    async def run(self) -> None:
        pruned = 0.0
        try:
            while True:
                if asyncio.get_running_loop().time() - pruned > 3600:
                    pruned = asyncio.get_running_loop().time()
                    try:
                        prune()
                    except Exception:
                        log.warning("couldn't prune old notices", exc_info=True)
                try:
                    with db.session() as s:
                        chans = s.exec(select(NotificationChannel).where(
                            NotificationChannel.enabled == True,  # noqa: E712
                            NotificationChannel.kind.in_(CHAT_KINDS))).all()
                    wanted = {c.id: c for c in chans}
                    for cid, (fp, task) in list(self.tasks.items()):
                        if cid not in wanted or fp != self._fingerprint(wanted[cid]) or task.done():
                            task.cancel()
                            # Let it let go of its lock first, so a restart takes over at once.
                            await asyncio.gather(task, return_exceptions=True)
                            del self.tasks[cid]
                    for cid, ch in wanted.items():
                        if cid not in self.tasks:
                            self.tasks[cid] = (self._fingerprint(ch), asyncio.create_task(self._hold(ch)))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("channel listeners: refresh failed")
                await asyncio.sleep(self.refresh)
        finally:
            for _, task in self.tasks.values():
                task.cancel()

    async def _hold(self, ch: NotificationChannel) -> None:
        lock = self.rt.broker.r.lock(f"ahq:lock:channel:{ch.id}", timeout=60, blocking_timeout=None)
        while not await lock.acquire(blocking=False):
            await asyncio.sleep(5)  # another API process listens; take over if it stops
        keeper = asyncio.create_task(self._keep(lock))
        listener = asyncio.create_task(driver_for(ch).listen(self.inbox))
        try:
            done, _ = await asyncio.wait({keeper, listener}, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t.exception():
                    log.warning("channel %s listener stopped: %s", ch.name, t.exception())
        finally:
            keeper.cancel()
            listener.cancel()
            await asyncio.gather(keeper, listener, return_exceptions=True)
            try:
                await lock.release()
            except Exception:
                pass

    @staticmethod
    async def _keep(lock) -> None:
        while True:
            await asyncio.sleep(20)
            await lock.extend(60, replace_ttl=True)
