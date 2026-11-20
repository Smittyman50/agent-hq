"""Telegram: a bot you create with @BotFather.

Out: notices as messages, with buttons for the decisions they allow. In: long polling
(getUpdates), so Agent HQ needs no address reachable from the internet. Button taps, replies
to notices and other messages are handed to the inbox (app/notifications.py), which checks
the sender is paired before doing anything.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import Any

import httpx

from ..config import settings
from . import ChannelError, Driver, chunks, footer

log = logging.getLogger("agent_hq.channels.telegram")

_TOKEN_IN_URL = re.compile(r"/bot\d+:[A-Za-z0-9_-]+")


class _RedactToken(logging.Filter):
    """httpx logs every request's URL, and the Bot API puts the token in the URL."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "/bot" in msg:
            record.msg, record.args = _TOKEN_IN_URL.sub("/bot<token>", msg), ()
        return True


for _name in ("httpx", "httpcore"):
    if not any(isinstance(f, _RedactToken) for f in logging.getLogger(_name).filters):
        logging.getLogger(_name).addFilter(_RedactToken())

LIMIT = 4096        # characters in one Telegram message
POLL_SECONDS = 50   # long-poll wait for getUpdates
ACTION_PREFIX = "n:"  # callback_data = n:<notice id>:<action id> (64 bytes at most)


class TelegramError(ChannelError):
    def __init__(self, message: str, code: int | None = None, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def sender_name(user: dict[str, Any]) -> str:
    name = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p)
    return name or user.get("username") or str(user.get("id", ""))


class TelegramDriver(Driver):
    kind = "telegram"
    label = "Telegram"
    chat = True

    def _token(self) -> str:
        from ..webhooks import unseal

        token = unseal(self.config.get("token") or "")
        if not token:
            raise ChannelError("no bot token")
        return token

    async def call(self, method: str, params: dict[str, Any] | None = None, *,
                   http_timeout: float = 20) -> Any:
        url = f"{settings.telegram_api_url}/bot{self._token()}/{method}"
        try:
            async with httpx.AsyncClient(timeout=http_timeout) as c:
                r = await c.post(url, json=params or {})
        except httpx.HTTPError as e:
            # Never the exception text: it has the URL, and the URL has the token.
            raise ChannelError(f"Telegram unreachable ({type(e).__name__})") from None
        try:
            data = r.json()
        except ValueError:
            raise TelegramError(f"Telegram answered HTTP {r.status_code}", r.status_code) from None
        if not data.get("ok"):
            raise TelegramError(data.get("description") or f"HTTP {r.status_code}",
                                data.get("error_code"), (data.get("parameters") or {}).get("retry_after"))
        return data.get("result")

    async def check(self) -> dict[str, Any]:
        me = await self.call("getMe")
        return {"bot_username": me.get("username", ""), "bot_name": me.get("first_name", "")}

    # ------------------------------------------------------------------ out

    def render(self, notice, *, decisions: bool, body: str | None = None) -> str:
        lines = [f"<b>{_esc(notice.title)}</b>"] if notice.title else []
        text = notice.body if body is None else body
        tail = "\n\n".join(_esc(line) for line in footer(notice, decisions=decisions))
        room = LIMIT - len("\n".join(lines)) - len(tail) - 40
        if text:
            esc = _esc(text)
            if len(esc) > room:
                esc = esc[:max(room, 0)].rstrip() + "…"
            lines.append(esc)
        out = "\n".join(lines)
        return out + ("\n\n" + tail if tail else "")

    def keyboard(self, notice, *, decisions: bool) -> dict | None:
        if not (decisions and notice.actions and notice.status == "open"):
            return None
        return {"inline_keyboard": [[
            {"text": a["label"], "callback_data": f"{ACTION_PREFIX}{notice.id}:{a['id']}"[:64]}
            for a in notice.actions]]}

    async def send(self, notice, chat_id: str, *, decisions: bool) -> str | None:
        if notice.type == "agent_reply" and len(notice.body or "") > LIMIT - 200:
            # An agent's long answer: several messages. A reply to the last one continues it.
            parts = chunks(notice.body, 3500)
            last = None
            for i, part in enumerate(parts):
                text = (f"<b>{_esc(notice.title)}</b>\n" if i == 0 and notice.title else "") + _esc(part)
                last = await self._send(chat_id, text)
            return last
        return await self._send(chat_id, self.render(notice, decisions=decisions),
                                self.keyboard(notice, decisions=decisions))

    async def _send(self, chat_id: str, text: str, markup: dict | None = None,
                    reply_to: str | None = None) -> str:
        params: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                  "link_preview_options": {"is_disabled": True}}
        if markup:
            params["reply_markup"] = markup
        if reply_to:
            params["reply_parameters"] = {"message_id": int(reply_to), "allow_sending_without_reply": True}
        msg = await self.call("sendMessage", params)
        return str(msg.get("message_id", ""))

    async def settle(self, notice, chat_id: str, message_id: str) -> None:
        try:
            await self.call("editMessageText", {
                "chat_id": chat_id, "message_id": int(message_id), "parse_mode": "HTML",
                "text": self.render(notice, decisions=True),
                "link_preview_options": {"is_disabled": True}})
        except TelegramError as e:
            if "not modified" not in str(e):
                raise

    async def send_text(self, chat_id: str, text: str, *, reply_to: str | None = None) -> str | None:
        return await self._send(chat_id, _esc(text)[:LIMIT], reply_to=reply_to)

    async def acknowledge(self, chat_id: str, message_id: str) -> None:
        try:
            await self.call("setMessageReaction", {
                "chat_id": chat_id, "message_id": int(message_id),
                "reaction": [{"type": "emoji", "emoji": "👍"}]})
        except ChannelError:
            pass  # older Bot API servers, or reactions turned off in that chat

    # ------------------------------------------------------------------ in

    async def listen(self, inbox) -> None:
        """Long-poll for updates until cancelled. The offset is kept in Valkey, so a restart
        neither loses nor repeats updates."""
        try:
            await self.call("deleteWebhook")  # getUpdates refuses to run while a webhook is set
        except ChannelError:
            pass
        offset = await inbox.get_offset(self.row.id)
        backoff = 5
        while True:
            try:
                updates = await self.call(
                    "getUpdates", {"offset": offset, "timeout": POLL_SECONDS,
                                   "allowed_updates": ["message", "callback_query"]},
                    http_timeout=POLL_SECONDS + 15)
                await inbox.channel_ok(self.row.id)
                backoff = 5
            except asyncio.CancelledError:
                raise
            except TelegramError as e:
                wait = e.retry_after or (60 if e.code in (401, 404) else backoff)
                if e.code == 409:
                    msg = "another program is reading this bot's updates (a second Agent HQ, or a webhook)"
                elif e.code in (401, 404):
                    msg = "Telegram rejected the bot token"
                else:
                    msg = str(e)
                await inbox.channel_error(self.row.id, msg)
                await asyncio.sleep(wait)
                backoff = min(backoff * 2, 60)
                continue
            except ChannelError as e:
                await inbox.channel_error(self.row.id, str(e))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            for u in updates or []:
                offset = int(u.get("update_id", 0)) + 1
                await inbox.set_offset(self.row.id, offset)
                try:
                    await self.handle(u, inbox)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("telegram update %s failed", u.get("update_id"))

    async def handle(self, update: dict[str, Any], inbox) -> None:
        from ..notifications import Sender

        if cq := update.get("callback_query"):
            frm, msg = cq.get("from") or {}, cq.get("message") or {}
            sender = Sender(user_id=str(frm.get("id", "")), chat_id=str((msg.get("chat") or {}).get("id", "")),
                            name=sender_name(frm))
            data = cq.get("data") or ""
            text = "That button isn't one of Agent HQ's."
            if data.startswith(ACTION_PREFIX):
                notice_id, _, action = data[len(ACTION_PREFIX):].partition(":")
                text = await inbox.on_action(self.row.id, sender, notice_id, action)
            with _suppress():
                await self.call("answerCallbackQuery", {"callback_query_id": cq.get("id"),
                                                        "text": (text or "")[:190]})
            return
        m = update.get("message")
        if not m or not m.get("from") or m["from"].get("is_bot"):
            return
        chat_id = str((m.get("chat") or {}).get("id", ""))
        sender = Sender(user_id=str(m["from"].get("id", "")), chat_id=chat_id, name=sender_name(m["from"]))
        reply_to = (m.get("reply_to_message") or {}).get("message_id")
        files = any(m.get(k) for k in ("photo", "document", "voice", "video", "audio", "sticker"))
        out = await inbox.on_message(self.row.id, sender, m.get("text") or m.get("caption") or "",
                                     reply_to=str(reply_to) if reply_to else None,
                                     message_id=str(m.get("message_id", "")), has_files=files)
        if out is None:
            return
        if out.text:
            with _suppress():
                await self.send_text(chat_id, out.text, reply_to=str(m.get("message_id", "")))
        if out.ack:
            await self.acknowledge(chat_id, str(m.get("message_id", "")))


class _suppress:
    """Ignore a failed answer: the update itself was handled."""

    def __enter__(self):
        return self

    def __exit__(self, kind, exc, tb):
        if exc is not None and isinstance(exc, ChannelError):
            log.warning("telegram: %s", exc)
            return True
        return False
