"""MQTT: every notice's event, as JSON, on the notification topic (outbound only).

The payload is the same one Agent HQ has always published (the event plus "at"), so
dashboards and automations that read AHQ_MQTT_NOTIFY_TOPIC keep working.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

from ..config import settings
from . import ChannelError, Driver


class MqttDriver(Driver):
    kind = "mqtt"
    label = "MQTT"

    @property
    def topic(self) -> str:
        return (self.config.get("topic") or "").strip() or settings.mqtt_notify_topic

    async def check(self) -> dict:
        if not settings.mqtt_url:
            raise ChannelError("AHQ_MQTT_URL isn't set, so there's no broker to publish to")
        return {"topic": self.topic}

    async def send(self, notice, chat_id: str, *, decisions: bool) -> str | None:
        if not settings.mqtt_url:
            raise ChannelError("AHQ_MQTT_URL isn't set")
        import aiomqtt

        from ..mqtt_bridge import client_args

        args = client_args(settings.mqtt_url)
        # A client ID of its own: reusing the bridge's would kick the bridge off the broker.
        args["identifier"] = f"agent-hq-notify-{settings.worker_name}-{uuid.uuid4().hex[:6]}"
        event = dict(notice.event or {})
        payload = json.dumps({**event, "at": event.get("at") or datetime.now(timezone.utc).isoformat()},
                             default=str)
        try:
            async with asyncio.timeout(10):
                async with aiomqtt.Client(**args) as client:
                    await client.publish(self.topic, payload, qos=1)
        except Exception as e:
            raise ChannelError(f"MQTT publish failed: {e}") from None
        return None
