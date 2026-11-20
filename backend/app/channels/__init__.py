"""Notification channel drivers: one class per kind of channel (MQTT, Telegram, Discord).

A driver only moves messages. What to send, who may answer and what an answer does all
live in app/notifications.py, so a new kind of channel is one small file here:

* send(notice, chat_id, decisions) -> the message ID (None if there's nothing to reply to)
* settle(notice, chat_id, message_id)  edit a sent notice once it's decided: show the
  outcome, drop the buttons
* send_text(chat_id, text, reply_to)  a plain answer to something you sent
* listen(inbox)  (chat channels) a long-lived loop that hands what you send to the inbox
* check()  is the configuration usable? e.g. the bot's name, from its token
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..models import Notice, NotificationChannel


class ChannelError(Exception):
    """A channel couldn't do what was asked (bad token, unreachable, refused)."""


class Driver:
    kind = ""
    label = ""
    # Takes answers: buttons, replies and chats (paired people only). MQTT doesn't.
    chat = False

    def __init__(self, row: NotificationChannel) -> None:
        self.row = row
        self.config: dict[str, Any] = dict(row.config or {})

    async def check(self) -> dict[str, Any]:
        return {}

    async def send(self, notice: Notice, chat_id: str, *, decisions: bool) -> str | None:
        raise NotImplementedError

    async def settle(self, notice: Notice, chat_id: str, message_id: str) -> None:
        return None

    async def send_text(self, chat_id: str, text: str, *, reply_to: str | None = None) -> str | None:
        return None

    async def acknowledge(self, chat_id: str, message_id: str) -> None:
        """Show that a message was taken (a reaction), where the channel can."""
        return None

    async def listen(self, inbox: Any) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        return None

    def invite_link(self) -> str:
        """Where to add the bot to a server (Discord); empty for other kinds."""
        return ""


def driver_for(row: NotificationChannel) -> Driver:
    from .discord import DiscordDriver
    from .mqtt import MqttDriver
    from .telegram import TelegramDriver

    kinds: dict[str, type[Driver]] = {"mqtt": MqttDriver, "telegram": TelegramDriver,
                                      "discord": DiscordDriver}
    cls = kinds.get(row.kind)
    if cls is None:
        raise ChannelError(f"unknown channel kind {row.kind!r}")
    return cls(row)


KINDS = ("mqtt", "telegram", "discord")
CHAT_KINDS = ("telegram", "discord")


# --------------------------------------------------------------------------- shared rendering

def footer(notice: Notice, *, decisions: bool) -> list[str]:
    """Lines under a notice's text: where to open or decide it, and its outcome."""
    if notice.type == "agent_reply":
        return []  # a chat with an agent: no link under every answer
    lines: list[str] = []
    open_request = bool(notice.actions) and notice.status == "open"
    if notice.link:
        lines.append(f"{'Decide' if open_request and not decisions else 'Open'} in Agent HQ: {notice.link}")
    elif open_request and not decisions:
        lines.append("Decide it in Agent HQ (Requests).")
    if notice.status == "settled" and notice.outcome:
        lines.append(notice.outcome)
    return lines


def chunks(text: str, size: int) -> list[str]:
    """Split long text on paragraph, then line, then word breaks."""
    out: list[str] = []
    rest = text
    while len(rest) > size:
        cut = max(rest.rfind("\n\n", 0, size), rest.rfind("\n", 0, size))
        if cut < size // 2:
            cut = rest.rfind(" ", 0, size)
        if cut < size // 2:
            cut = size
        out.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest or not out:
        out.append(rest)
    return out
