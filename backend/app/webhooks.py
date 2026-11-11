"""Inbound webhooks -> agents.

Any system on the LAN that can send an HTTP POST can wake an agent:

    POST /hooks/<name>   ->  event  webhook/<source>/<event>  ->  Events subscriptions

The event goes through the same router as MQTT (mqtt_bridge.handle_message), so
subscriptions, cooldowns, prompt templates and report conversations work the same way.
The hook answers 202 straight away; the agent's turn runs on a worker afterwards.

/hooks/* is outside the sign-in (app/api/auth.py protects /api and /ws only), so each
hook checks its sender itself:
  * gitea: X-Gitea-Signature, the hex HMAC-SHA256 of the body keyed with the hook's secret
           (what Gitea's webhook "Secret" field produces);
  * token: the secret in X-Webhook-Token, an "Authorization: Bearer" header, or ?token=.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
from typing import Any

from .models import WebhookEndpoint

log = logging.getLogger("agent_hq.webhooks")

AUTH_MODES = ("gitea", "token")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
PRESET_EVENT_HEADER = {"gitea": "X-Gitea-Event"}


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def gitea_signature(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def check_sender(hook: WebhookEndpoint, headers: dict[str, str], query: dict[str, str],
                 body: bytes) -> bool:
    """True if the request proves it knows the hook's secret. `headers` keys are lowercase."""
    if not hook.secret:
        return False
    if hook.auth_mode == "gitea":
        sent = headers.get("x-gitea-signature", "").strip().lower()
        return bool(sent) and hmac.compare_digest(sent, gitea_signature(hook.secret, body))
    bearer = headers.get("authorization", "")
    sent = (headers.get("x-webhook-token")
            or (bearer[7:].strip() if bearer.lower().startswith("bearer ") else "")
            or query.get("token", ""))
    return bool(sent) and hmac.compare_digest(sent.encode(), hook.secret.encode())


def _topic_level(value: str) -> str:
    """One safe MQTT-style topic level: no '/', '+', '#' or spaces; lowercase."""
    v = re.sub(r"[^a-z0-9._-]+", "_", value.strip().lower()).strip("_")
    return v[:64] or "event"


def _field(data: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(data, dict) and part in data:
            data = data[part]
        elif isinstance(data, list) and part.isdigit() and int(part) < len(data):
            data = data[int(part)]
        else:
            return None
    return data


def event_name(hook: WebhookEndpoint, headers: dict[str, str], body: bytes) -> str:
    """The last topic level: from the event header, else a JSON field, else "event"."""
    header = (hook.event_header or PRESET_EVENT_HEADER.get(hook.auth_mode, "")).lower()
    if header and headers.get(header):
        return _topic_level(headers[header])
    if hook.event_field:
        try:
            value = _field(json.loads(body), hook.event_field)
        except (ValueError, UnicodeDecodeError):
            value = None
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            return _topic_level(str(value))
    return "event"


def topic_for(hook: WebhookEndpoint, headers: dict[str, str], body: bytes) -> str:
    return f"webhook/{_topic_level(hook.source or hook.name)}/{event_name(hook, headers, body)}"
