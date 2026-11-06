"""MQTT -> agents. Subscriptions (managed in the UI) map topic filters to agents.

When a message matches, the bridge queues a turn in the subscription's conversation,
so the agent can investigate, delegate, and report to you there. Runs inside the API
process; run only one API process with AHQ_MQTT_URL set, or events are handled twice.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import unquote, urlparse

import aiomqtt
from sqlmodel import select

from . import db
from .config import settings
from .models import AgentDef, Subscription, Thread
from .runtime import Runtime

log = logging.getLogger("agent_hq.mqtt")

DEFAULT_PROMPT = (
    "An event arrived on MQTT topic `{topic}`:\n\n{payload}\n\n"
    "Assess it. If it needs attention, investigate or delegate as appropriate and report "
    "what you found and what you recommend. If it is routine, say so in one line.")


def topic_matches(topic_filter: str, topic: str) -> bool:
    """MQTT wildcard matching: + matches one level, # matches the rest."""
    f_parts, t_parts = topic_filter.split("/"), topic.split("/")
    for i, f in enumerate(f_parts):
        if f == "#":
            return True
        if i >= len(t_parts):
            return False
        if f != "+" and f != t_parts[i]:
            return False
    return len(f_parts) == len(t_parts)


def render_prompt(sub: Subscription, topic: str, payload: str) -> str:
    template = sub.prompt_template or DEFAULT_PROMPT
    return template.replace("{topic}", topic).replace("{payload}", payload)


def load_subscriptions() -> list[Subscription]:
    with db.session() as s:
        return list(s.exec(select(Subscription).where(Subscription.enabled == True)).all())  # noqa: E712


def ensure_thread(sub: Subscription) -> str:
    """The subscription's conversation, created on first use."""
    with db.session() as s:
        row = s.get(Subscription, sub.id)
        if row.thread_id and s.get(Thread, row.thread_id):
            return row.thread_id
        agent = s.get(AgentDef, row.agent_id)
        t = Thread(agent_id=row.agent_id, title=f"Events: {row.name} ({agent.name if agent else '?'})")
        s.add(t)
        s.commit()
        row.thread_id = t.id
        s.add(row)
        s.commit()
        return t.id


async def handle_message(rt: Runtime, topic: str, raw: bytes) -> list[str]:
    """Route one MQTT message. Returns the IDs of the turn jobs queued."""
    payload = raw.decode("utf-8", errors="replace")
    if len(payload) > settings.mqtt_max_payload:
        payload = payload[: settings.mqtt_max_payload] + "\n…[truncated]"
    queued = []
    for sub in load_subscriptions():
        if not topic_matches(sub.topic_filter, topic):
            continue
        if not await rt.broker.try_cooldown(sub.id, sub.min_interval_seconds):
            log.info("subscription %s in cooldown; skipping %s", sub.name, topic)
            continue
        thread_id = ensure_thread(sub)
        job = await rt.enqueue_turn(
            thread_id, render_prompt(sub, topic, payload), from_user=False,
            event_text=f"MQTT {topic}: {payload[:500]}", kind="mqtt", topic=topic,
            subscription=sub.name)
        queued.append(job.id)
    return queued


def client_args(url: str) -> dict:
    u = urlparse(url)
    tls = u.scheme in ("mqtts", "ssl")
    args = {"hostname": u.hostname or "localhost", "port": u.port or (8883 if tls else 1883),
            "identifier": f"agent-hq-{settings.worker_name}"}
    if u.username:
        args["username"] = unquote(u.username)
        args["password"] = unquote(u.password or "")
    if tls:
        args["tls_params"] = aiomqtt.TLSParameters()
    return args


class MqttBridge:
    def __init__(self, rt: Runtime, url: str, refresh_seconds: float = 15) -> None:
        self.rt = rt
        self.url = url
        self.refresh_seconds = refresh_seconds
        self.connected = asyncio.Event()

    async def run(self) -> None:
        while True:
            try:
                async with aiomqtt.Client(**client_args(self.url)) as client:
                    log.info("MQTT connected to %s", urlparse(self.url).hostname)
                    current: set[str] = set()
                    syncer = asyncio.create_task(self._sync_filters(client, current))
                    try:
                        async for msg in client.messages:
                            try:
                                await handle_message(self.rt, str(msg.topic), bytes(msg.payload or b""))
                            except Exception:
                                log.exception("failed to handle MQTT message on %s", msg.topic)
                    finally:
                        syncer.cancel()
                        self.connected.clear()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("MQTT connection problem (%s); reconnecting in 5s", e)
                await asyncio.sleep(5)

    async def _sync_filters(self, client: aiomqtt.Client, current: set[str]) -> None:
        """Keep broker subscriptions in step with the Subscription table."""
        while True:
            wanted = {s.topic_filter for s in load_subscriptions()}
            for f in wanted - current:
                await client.subscribe(f, qos=1)
            for f in current - wanted:
                await client.unsubscribe(f)
            current.clear()
            current.update(wanted)
            self.connected.set()
            await asyncio.sleep(self.refresh_seconds)
