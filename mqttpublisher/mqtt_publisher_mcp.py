"""MQTT publisher: lets Agent HQ agents publish status and commands back to the lab, as an MCP server.

Tools:
  list_publish_topics()                      where this agent may publish, and the limits
  publish_status(name, state, detail, data)  a retained JSON status at <prefix>/<name>/status
  publish(topic, payload, qos, retain)       any topic on the allow-list (asks you first)

Agent HQ already listens on MQTT (Settings -> Events) and sends you notifications there.
This is the other direction: an agent reporting "backup check done, 2 warnings" for a
Home Assistant dashboard, or sending a command to something in the lab that listens on MQTT.

Listens on http://0.0.0.0:8810/mcp. Register that URL under Settings -> Tools (untick
"Prefix tool names", leave "Controls an agent desktop" unticked) and tick it on the agents
that should publish.

Settings (environment variables):
  MQTT_PUBLISH_URL             broker, e.g. mqtt://agent-hq-pub:secret@broker:1883 (mqtts:// for TLS)
  MQTT_PUBLISH_CA_FILE         CA certificate for mqtts:// (e.g. your step-ca root); default: system CAs
  MQTT_PUBLISH_ALLOW           comma-separated topic filters `publish` may use (default agent-hq/agents/#)
  MQTT_PUBLISH_DENY            filters that are always refused, even if allowed
                               (default agent-hq/notifications/#)
  MQTT_PUBLISH_STATUS_PREFIX   where publish_status writes (default agent-hq/agents)
  MQTT_PUBLISH_MAX_BYTES       largest payload, in bytes (default 16384)
  MQTT_PUBLISH_RATE_PER_MINUTE most messages per minute across all agents (default 30)

Safety: topics starting with `$` and topics with wildcards are refused; `publish` asks for
your approval by default ("agent_hq/approval": "ask"), `publish_status` doesn't. Change either
per tool under Settings -> Tools -> Approvals. Give the broker user write access only to the
topics you allow here, so the broker enforces the same limits.

Watch for loops: if an Events subscription in Agent HQ matches a topic agents publish to,
each publish wakes an agent again. Keep publish topics outside your subscription filters.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import ssl
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote, urlparse

import aiomqtt
from fastmcp import FastMCP


def _filters(value: str) -> list[str]:
    return [f.strip() for f in value.split(",") if f.strip()]


MQTT_URL = os.environ.get("MQTT_PUBLISH_URL", "")
CA_FILE = os.environ.get("MQTT_PUBLISH_CA_FILE", "")
ALLOW = _filters(os.environ.get("MQTT_PUBLISH_ALLOW", "agent-hq/agents/#"))
DENY = _filters(os.environ.get("MQTT_PUBLISH_DENY", "agent-hq/notifications/#"))
STATUS_PREFIX = os.environ.get("MQTT_PUBLISH_STATUS_PREFIX", "agent-hq/agents").strip().strip("/")
MAX_BYTES = int(os.environ.get("MQTT_PUBLISH_MAX_BYTES", "16384"))
RATE_PER_MINUTE = int(os.environ.get("MQTT_PUBLISH_RATE_PER_MINUTE", "30"))
TIMEOUT = 10.0

ASK = {"agent_hq/approval": "ask"}
ALLOW_META = {"agent_hq/approval": "allow"}

mcp = FastMCP("mqtt-publisher", instructions=(
    "Publish to the home lab's MQTT broker. Use publish_status to report your own state "
    "(retained, so dashboards show the latest), and publish only for topics on the allow-list "
    "(call list_publish_topics to see it). Publish only what your task calls for; never "
    "publish secrets."))


class PublishError(Exception):
    pass


# --------------------------------------------------------------------------- topics

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


def check_topic(topic: str, *, allow: list[str] | None) -> str:
    """Refuse malformed, system and denied topics; if `allow` is given, require a match."""
    topic = (topic or "").strip()
    if not topic:
        raise PublishError("topic is empty")
    if "+" in topic or "#" in topic:
        raise PublishError(f"{topic!r} has a wildcard; publish to one exact topic")
    if topic.startswith("$"):
        raise PublishError(f"{topic!r} is a broker system topic")
    if "\x00" in topic or len(topic.encode()) > 1024:
        raise PublishError("topic has a NUL character or is longer than 1024 bytes")
    for f in DENY:
        if topic_matches(f, topic):
            raise PublishError(f"{topic!r} is reserved (MQTT_PUBLISH_DENY: {f})")
    if allow is not None and not any(topic_matches(f, topic) for f in allow):
        raise PublishError(f"{topic!r} is not on the allow-list ({', '.join(allow) or 'empty'}); "
                           "ask the user to add it to MQTT_PUBLISH_ALLOW")
    return topic


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9_-]+", "-", (name or "").strip().lower()).strip("-")
    if not s:
        raise PublishError("name is empty")
    return s[:64]


def encode(payload: Any) -> bytes:
    if payload is None:
        data = b""
    elif isinstance(payload, (bytes, bytearray)):
        data = bytes(payload)
    elif isinstance(payload, str):
        data = payload.encode()
    else:
        data = json.dumps(payload, default=str, separators=(",", ":")).encode()
    if len(data) > MAX_BYTES:
        raise PublishError(f"payload is {len(data)} bytes; the limit is {MAX_BYTES} (MQTT_PUBLISH_MAX_BYTES)")
    return data


# --------------------------------------------------------------------------- broker

_sent: deque[float] = deque()
_rate_lock = asyncio.Lock()


async def _take_slot() -> None:
    async with _rate_lock:
        now = time.monotonic()
        while _sent and now - _sent[0] > 60:
            _sent.popleft()
        if len(_sent) >= RATE_PER_MINUTE:
            wait = int(60 - (now - _sent[0])) + 1
            raise PublishError(f"rate limit reached ({RATE_PER_MINUTE}/minute); try again in {wait}s")
        _sent.append(now)


def client_args(url: str) -> dict:
    u = urlparse(url)
    if u.scheme not in ("mqtt", "mqtts", "tcp", "ssl"):
        raise PublishError(f"MQTT_PUBLISH_URL must be mqtt:// or mqtts://, not {u.scheme}://")
    tls = u.scheme in ("mqtts", "ssl")
    args: dict[str, Any] = {
        "hostname": u.hostname or "localhost", "port": u.port or (8883 if tls else 1883),
        # A fresh ID per connection, so concurrent publishes never kick each other off.
        "identifier": f"agent-hq-publisher-{uuid.uuid4().hex[:8]}"}
    if u.username:
        args["username"] = unquote(u.username)
        args["password"] = unquote(u.password or "")
    if tls:
        args["tls_context"] = ssl.create_default_context(cafile=CA_FILE or None)
    return args


async def send(topic: str, data: bytes, qos: int, retain: bool) -> None:
    if not MQTT_URL:
        raise PublishError("no broker configured (set MQTT_PUBLISH_URL on the mqtt-publisher service)")
    if qos not in (0, 1, 2):
        raise PublishError("qos must be 0, 1 or 2")
    await _take_slot()
    try:
        async with asyncio.timeout(TIMEOUT):
            async with aiomqtt.Client(**client_args(MQTT_URL)) as client:
                await client.publish(topic, data, qos=qos, retain=retain)
    except TimeoutError as e:
        raise PublishError(f"the broker didn't answer within {TIMEOUT:.0f}s") from e
    except aiomqtt.MqttError as e:
        raise PublishError(f"broker refused or dropped the publish: {e}") from e


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- tools

@mcp.tool(meta=ALLOW_META, annotations={"readOnlyHint": True})
def list_publish_topics() -> dict:
    """Where you may publish: the allow-list for `publish`, reserved topics, your status topic
    pattern, and the size and rate limits."""
    return {"allowed": ALLOW, "denied": DENY,
            "status_topic": f"{STATUS_PREFIX}/<name>/status",
            "max_payload_bytes": MAX_BYTES, "rate_per_minute": RATE_PER_MINUTE,
            "broker_configured": bool(MQTT_URL)}


@mcp.tool(meta=ALLOW_META)
async def publish_status(name: str, state: str, detail: str = "", data: dict | None = None,
                         retain: bool = True) -> dict:
    """Report your state on MQTT, e.g. for a Home Assistant dashboard.

    Publishes JSON {"agent", "state", "detail", "data", "at"} to <prefix>/<name>/status, retained
    by default so subscribers see the latest state straight away. Use your own agent name as
    `name`; `state` is a short word such as ok, working, warning, error or idle; `detail` is one
    line for people; `data` holds any numbers worth graphing.
    """
    try:
        topic = check_topic(f"{STATUS_PREFIX}/{slug(name)}/status", allow=None)
        body = encode({"agent": name.strip(), "state": (state or "").strip()[:40],
                       "detail": (detail or "")[:500], "data": data or {}, "at": _now()})
        await send(topic, body, qos=1, retain=retain)
    except PublishError as e:
        return {"error": str(e)}
    return {"published": True, "topic": topic, "bytes": len(body), "retained": retain}


@mcp.tool(meta=ASK)
async def publish(topic: str, payload: str | dict | list | None = None, qos: int = 1,
                  retain: bool = False) -> dict:
    """Publish a message to one MQTT topic on the allow-list (see list_publish_topics).

    `payload` is sent as-is when it's text; objects and lists are sent as JSON. Use this for
    commands and messages other systems listen for. Set `retain` only for state that new
    subscribers should see (it stays on the broker until replaced). qos: 0, 1 (default) or 2.
    """
    try:
        topic = check_topic(topic, allow=ALLOW)
        body = encode(payload)
        await send(topic, body, qos=int(qos), retain=bool(retain))
    except PublishError as e:
        return {"error": str(e)}
    return {"published": True, "topic": topic, "bytes": len(body), "qos": int(qos), "retained": bool(retain)}


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("MQTT_PUBLISH_PORT", "8810")))
