"""Discord: a bot you create in the Discord Developer Portal.

Out: notices as embeds, with buttons for the decisions they allow; agents' answers as plain
messages. In: the Gateway, a websocket Agent HQ opens to Discord, so nothing has to reach
Agent HQ from the internet. Button presses (interactions), DMs to the bot, messages that
@mention it and replies to its messages are handed to the inbox (app/notifications.py),
which checks the sender is paired before doing anything.

Without the privileged Message Content intent, Discord only shows a bot the text of DMs,
messages that mention it and replies to its messages (with "ping on reply" on). That is
all this needs: DM the bot, or mention it in a server channel. Turn on Message Content
(here and in the Developer Portal) to talk to it in a paired server channel without a mention.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from typing import Any
from urllib.parse import quote

import httpx

from ..config import settings
from . import ChannelError, Driver, chunks, footer

log = logging.getLogger("agent_hq.channels.discord")

CONTENT_LIMIT = 2000   # characters in a message
EMBED_LIMIT = 4096     # characters in an embed's description
ACTION_PREFIX = "n:"   # custom_id = n:<notice id>:<action id> (100 characters at most)

# Gateway intents: server messages and DMs (not privileged), and Message Content (privileged,
# only when turned on).
GUILD_MESSAGES = 1 << 9
DIRECT_MESSAGES = 1 << 12
MESSAGE_CONTENT = 1 << 15

FATAL_CLOSE = {4004: "Discord rejected the bot token",
               4013: "Discord refused the gateway intents",
               4014: "Message Content isn't enabled for this bot in the Discord Developer Portal "
                     "(Bot → Privileged Gateway Intents), or turn it off here"}

COLOR = {"request": 0x6366F1, "info": 0x3B82F6, "warning": 0xF59E0B, "critical": 0xEF4444,
         "settled": 0x6B7280, "test": 0x22C55E}
STYLE = {"approve": 3, "done": 3, "ready": 3, "backlog": 1, "deny": 4}  # success, primary, danger


class DiscordError(ChannelError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def user_name(user: dict[str, Any]) -> str:
    return user.get("global_name") or user.get("username") or str(user.get("id", ""))


class DiscordDriver(Driver):
    kind = "discord"
    label = "Discord"
    chat = True

    def _token(self) -> str:
        from ..webhooks import unseal

        token = unseal(self.config.get("token") or "")
        if not token:
            raise ChannelError("no bot token")
        return token

    async def call(self, method: str, path: str, body: Any = None, *, auth: bool = True) -> Any:
        """One REST call; waits out a rate limit once."""
        headers = {"User-Agent": "DiscordBot (https://github.com/Smittyman50/agent-hq, 1)"}
        if auth:
            headers["Authorization"] = f"Bot {self._token()}"
        url = f"{settings.discord_api_url}{path}"
        for attempt in (1, 2):
            try:
                async with httpx.AsyncClient(timeout=20) as c:
                    r = await c.request(method, url, headers=headers,
                                        **({"json": body} if body is not None else {}))
            except httpx.HTTPError as e:
                raise ChannelError(f"Discord unreachable ({type(e).__name__})") from None
            if r.status_code == 429 and attempt == 1:
                try:
                    wait = float(r.json().get("retry_after", 1))
                except ValueError:
                    wait = 1.0
                await asyncio.sleep(min(wait, 10))
                continue
            break
        if r.status_code == 204:
            return None
        try:
            data = r.json()
        except ValueError:
            data = None
        if r.status_code >= 400:
            msg = (data or {}).get("message") if isinstance(data, dict) else None
            if r.status_code == 401:
                msg = "Discord rejected the bot token"
            raise DiscordError(msg or f"Discord answered HTTP {r.status_code}", r.status_code)
        return data

    async def check(self) -> dict[str, Any]:
        me = await self.call("GET", "/users/@me")
        try:
            app = await self.call("GET", "/oauth2/applications/@me")
            app_id = str(app.get("id", ""))
        except DiscordError:
            app_id = str(me.get("id", ""))  # a bot's user ID is its application's ID
        return {"bot_username": me.get("username", ""), "bot_id": str(me.get("id", "")),
                "application_id": app_id}

    def invite_link(self) -> str:
        app = self.config.get("application_id")
        # View Channel, Send Messages, Embed Links, Read Message History, Add Reactions.
        perms = (1 << 10) | (1 << 11) | (1 << 14) | (1 << 16) | (1 << 6)
        return (f"https://discord.com/oauth2/authorize?client_id={app}&scope=bot&permissions={perms}"
                if app else "")

    # ------------------------------------------------------------------ out

    def embed(self, notice, *, decisions: bool) -> dict[str, Any]:
        lines = footer(notice, decisions=decisions)
        tail = "\n\n".join(lines)
        body = notice.body or ""
        room = EMBED_LIMIT - len(tail) - 10
        if len(body) > room:
            body = body[:max(room, 0)].rstrip() + "…"
        desc = (body + ("\n\n" + tail if tail else "")).strip()
        group = "settled" if notice.status == "settled" else (
            notice.severity or ("test" if notice.type == "test" else
                                "request" if notice.actions else "info"))
        out: dict[str, Any] = {"title": (notice.title or "Agent HQ")[:256], "color": COLOR.get(group, COLOR["info"])}
        if desc:
            out["description"] = desc
        return out

    def components(self, notice, *, decisions: bool) -> list[dict[str, Any]]:
        if not (decisions and notice.actions and notice.status == "open"):
            return []
        return [{"type": 1, "components": [
            {"type": 2, "style": STYLE.get(a["id"], 2), "label": a["label"][:80],
             "custom_id": f"{ACTION_PREFIX}{notice.id}:{a['id']}"[:100]} for a in notice.actions[:5]]}]

    async def send(self, notice, chat_id: str, *, decisions: bool) -> str | None:
        if notice.type == "agent_reply":
            # An agent's answer: plain text (Discord renders its Markdown). A reply to the
            # last part continues the conversation.
            head = f"**{notice.title}**\n" if notice.title else ""
            parts = chunks(notice.body or "(no reply)", CONTENT_LIMIT - len(head) - 10)
            last = None
            for i, part in enumerate(parts):
                last = await self._post(chat_id, {"content": (head if i == 0 else "") + part})
            return last
        return await self._post(chat_id, {"embeds": [self.embed(notice, decisions=decisions)],
                                          "components": self.components(notice, decisions=decisions)})

    async def _post(self, chat_id: str, body: dict[str, Any], reply_to: str | None = None) -> str:
        body = {**body, "allowed_mentions": {"parse": []}}  # an agent's text never pings anyone
        if reply_to:
            body["message_reference"] = {"message_id": reply_to, "fail_if_not_exists": False}
        msg = await self.call("POST", f"/channels/{chat_id}/messages", body)
        return str((msg or {}).get("id", ""))

    async def settle(self, notice, chat_id: str, message_id: str) -> None:
        await self.call("PATCH", f"/channels/{chat_id}/messages/{message_id}",
                        {"embeds": [self.embed(notice, decisions=True)], "components": []})

    async def send_text(self, chat_id: str, text: str, *, reply_to: str | None = None) -> str | None:
        return await self._post(chat_id, {"content": text[:CONTENT_LIMIT]}, reply_to=reply_to)

    async def acknowledge(self, chat_id: str, message_id: str) -> None:
        try:
            await self.call("PUT", f"/channels/{chat_id}/messages/{message_id}/reactions/{quote('👍')}/@me")
        except ChannelError:
            pass  # no Add Reactions permission in that channel

    # ------------------------------------------------------------------ in

    def intents(self) -> int:
        return GUILD_MESSAGES | DIRECT_MESSAGES | (MESSAGE_CONTENT if self.config.get("message_content") else 0)

    async def listen(self, inbox) -> None:
        """Keep a Gateway connection open until cancelled, resuming after drops so events
        aren't missed. A token or intent Discord refuses is reported and retried slowly."""
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed

        session: dict[str, Any] = {"id": None, "seq": None, "resume_url": None}
        backoff = 2
        while True:
            try:
                if session["id"] and session["resume_url"]:
                    url = session["resume_url"]
                else:
                    url = (await self.call("GET", "/gateway/bot"))["url"]
                url = url.rstrip("/") + "/?v=10&encoding=json"
                async with connect(url, max_size=2 ** 22, open_timeout=20) as ws:
                    await self._session(ws, inbox, session)
                backoff = 2
            except asyncio.CancelledError:
                raise
            except ConnectionClosed as e:
                code = e.rcvd.code if e.rcvd else None
                if code in FATAL_CLOSE:
                    session.update(id=None, seq=None, resume_url=None)
                    await inbox.channel_error(self.row.id, FATAL_CLOSE[code])
                    await asyncio.sleep(60)
                    continue
                if code in (4007, 4009):  # bad sequence, session timed out: start a new one
                    session.update(id=None, seq=None, resume_url=None)
                log.info("discord gateway closed (%s); reconnecting", code)
            except DiscordError as e:
                await inbox.channel_error(self.row.id, str(e))
                await asyncio.sleep(60 if e.status == 401 else backoff)
            except Exception as e:  # network trouble, a timeout
                await inbox.channel_error(self.row.id, f"Discord gateway: {type(e).__name__}: {e}"[:300])
            await asyncio.sleep(backoff + random.random())
            backoff = min(backoff * 2, 60)

    async def _session(self, ws, inbox, session: dict[str, Any]) -> None:
        hello = json.loads(await asyncio.wait_for(ws.recv(), 20))
        if hello.get("op") != 10:
            raise ChannelError("Discord gateway didn't say hello")
        interval = hello["d"]["heartbeat_interval"] / 1000
        acked = asyncio.Event()
        acked.set()
        if session["id"]:
            await ws.send(json.dumps({"op": 6, "d": {"token": self._token(), "session_id": session["id"],
                                                     "seq": session["seq"]}}))
        else:
            await self._identify(ws)

        async def beat():
            await asyncio.sleep(interval * random.random())
            while True:
                if not acked.is_set():  # no answer to the last one: the connection is dead
                    await ws.close(4000, "heartbeat not acknowledged")
                    return
                acked.clear()
                await ws.send(json.dumps({"op": 1, "d": session["seq"]}))
                await asyncio.sleep(interval)

        beater = asyncio.create_task(beat())
        try:
            async for raw in ws:
                msg = json.loads(raw)
                op = msg.get("op")
                if msg.get("s") is not None:
                    session["seq"] = msg["s"]
                if op == 11:
                    acked.set()
                elif op == 1:
                    await ws.send(json.dumps({"op": 1, "d": session["seq"]}))
                elif op == 7:  # Discord asks for a reconnect (and resume)
                    await ws.close(4000, "reconnect requested")
                    return
                elif op == 9:  # invalid session: resumable only if d is true
                    if not msg.get("d"):
                        session.update(id=None, seq=None, resume_url=None)
                    await asyncio.sleep(1 + random.random() * 4)
                    if session["id"]:
                        await ws.send(json.dumps({"op": 6, "d": {
                            "token": self._token(), "session_id": session["id"], "seq": session["seq"]}}))
                    else:
                        await self._identify(ws)
                elif op == 0:
                    await self._dispatch(msg.get("t"), msg.get("d") or {}, inbox, session)
        finally:
            beater.cancel()

    async def _identify(self, ws) -> None:
        await ws.send(json.dumps({"op": 2, "d": {
            "token": self._token(), "intents": self.intents(),
            "properties": {"os": "linux", "browser": "agent-hq", "device": "agent-hq"}}}))

    async def _dispatch(self, kind: str | None, d: dict[str, Any], inbox, session: dict[str, Any]) -> None:
        if kind == "READY":
            session["id"], session["resume_url"] = d.get("session_id"), d.get("resume_gateway_url")
            self.config["bot_id"] = str((d.get("user") or {}).get("id") or self.config.get("bot_id", ""))
            await inbox.channel_ok(self.row.id)
        elif kind == "RESUMED":
            await inbox.channel_ok(self.row.id)
        elif kind == "INTERACTION_CREATE":
            task = asyncio.create_task(self._interaction(d, inbox))
            task.add_done_callback(_log_failure)
        elif kind == "MESSAGE_CREATE":
            task = asyncio.create_task(self._message(d, inbox))
            task.add_done_callback(_log_failure)

    async def _interaction(self, d: dict[str, Any], inbox) -> None:
        from ..notifications import Sender

        if d.get("type") != 3:  # only button presses (message components)
            return
        user = (d.get("member") or {}).get("user") or d.get("user") or {}
        sender = Sender(user_id=str(user.get("id", "")), chat_id=str(d.get("channel_id", "")),
                        name=user_name(user))
        # Answer within 3 seconds or Discord shows the press as failed: defer (only the
        # presser sees the answer), then fill it in when the decision is made.
        await self.call("POST", f"/interactions/{d['id']}/{d['token']}/callback",
                        {"type": 5, "data": {"flags": 64}}, auth=False)
        custom = (d.get("data") or {}).get("custom_id") or ""
        text = "That button isn't one of Agent HQ's."
        if custom.startswith(ACTION_PREFIX):
            notice_id, _, action = custom[len(ACTION_PREFIX):].partition(":")
            text = await inbox.on_action(self.row.id, sender, notice_id, action)
        app = d.get("application_id") or self.config.get("application_id")
        try:
            await self.call("PATCH", f"/webhooks/{app}/{d['token']}/messages/@original",
                            {"content": (text or "Done.")[:CONTENT_LIMIT]}, auth=False)
        except ChannelError as e:
            log.warning("discord: couldn't answer a button press: %s", e)

    def _addressed(self, d: dict[str, Any]) -> bool:
        bot = self.config.get("bot_id")
        if any(str(m.get("id")) == bot for m in d.get("mentions") or []):
            return True
        ref = d.get("referenced_message") or {}
        return str((ref.get("author") or {}).get("id", "")) == bot

    async def _message(self, d: dict[str, Any], inbox) -> None:
        author = d.get("author") or {}
        if author.get("bot") or str(author.get("id")) == self.config.get("bot_id"):
            return
        chat_id = str(d.get("channel_id", ""))
        in_server = bool(d.get("guild_id"))
        if in_server and not self._addressed(d):
            # A server channel: only messages to the bot, or anything in a paired chat once
            # Message Content shows their text. Everything else is people talking to each other.
            from ..notifications import _channel

            row = _channel(self.row.id)
            paired_here = row is not None and any(
                str(p.get("chat_id")) == chat_id and str(p.get("user_id")) == str(author.get("id"))
                for p in row.paired or [])
            if not (paired_here and d.get("content")):
                return
        from ..notifications import Sender

        text = re.sub(rf"<@!?{re.escape(self.config.get('bot_id') or '-')}>", "", d.get("content") or "").strip()
        if text.startswith("!"):
            text = "/" + text[1:]  # !new, !help, !pair CODE: Discord keeps / for its own commands
        reply_to = (d.get("message_reference") or {}).get("message_id")
        sender = Sender(user_id=str(author.get("id", "")), chat_id=chat_id, name=user_name(author))
        out = await inbox.on_message(self.row.id, sender, text, reply_to=str(reply_to) if reply_to else None,
                                     message_id=str(d.get("id", "")), has_files=bool(d.get("attachments")))
        if out is None:
            return
        if out.text:
            try:
                await self.send_text(chat_id, out.text, reply_to=str(d.get("id", "")))
            except ChannelError as e:
                log.warning("discord: %s", e)
        if out.ack:
            await self.acknowledge(chat_id, str(d.get("id", "")))


def _log_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception():
        log.warning("discord event failed", exc_info=task.exception())
