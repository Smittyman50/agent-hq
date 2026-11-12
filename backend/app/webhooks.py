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

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from .config import settings
from .models import WebhookEndpoint

log = logging.getLogger("agent_hq.webhooks")

AUTH_MODES = ("gitea", "token")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
PRESET_EVENT_HEADER = {"gitea": "X-Gitea-Event"}
PRESET_DELIVERY_HEADER = {"gitea": "X-Gitea-Delivery"}
_SECRET_HEADER = re.compile(r"authorization|cookie|token|signature|secret|api-key|apikey", re.I)


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def gitea_signature(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------- secrets at rest

SEALED = "enc:v1:"


class SecretUnavailable(Exception):
    """The hook's secret is encrypted and AHQ_SECRET_KEY is missing or different."""


@lru_cache(maxsize=4)
def _fernet_for(key: str) -> Fernet:
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError):  # any passphrase: derive a Fernet key from it
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))


def _fernet() -> Fernet | None:
    return _fernet_for(settings.secret_key) if settings.secret_key else None


def encryption_on() -> bool:
    return bool(settings.secret_key)


def seal(secret: str) -> str:
    """How a secret is stored: encrypted when AHQ_SECRET_KEY is set, else as is."""
    f = _fernet()
    if not f or not secret or secret.startswith(SEALED):
        return secret
    return SEALED + f.encrypt(secret.encode()).decode()


def unseal(stored: str) -> str:
    if not stored.startswith(SEALED):
        return stored
    f = _fernet()
    if f is None:
        raise SecretUnavailable("the secret is encrypted but AHQ_SECRET_KEY isn't set")
    try:
        return f.decrypt(stored[len(SEALED):].encode()).decode()
    except InvalidToken:
        raise SecretUnavailable("AHQ_SECRET_KEY can't decrypt the secret (was it changed?)") from None


def seal_existing() -> int:
    """At startup: encrypt secrets stored before AHQ_SECRET_KEY was set."""
    if not encryption_on():
        return 0
    from sqlmodel import select

    from . import db

    n = 0
    with db.session() as s:
        for h in s.exec(select(WebhookEndpoint)).all():
            if h.secret and not h.secret.startswith(SEALED):
                h.secret = seal(h.secret)
                s.add(h)
                n += 1
        if n:
            s.commit()
    if n:
        log.info("encrypted %d webhook secret(s)", n)
    return n


def check_sender(hook: WebhookEndpoint, headers: dict[str, str], query: dict[str, str],
                 body: bytes) -> bool:
    """True if the request proves it knows the hook's secret. `headers` keys are lowercase.
    Raises SecretUnavailable if the stored secret can't be decrypted."""
    if not hook.secret:
        return False
    secret = unseal(hook.secret)
    if hook.auth_mode == "gitea":
        sent = headers.get("x-gitea-signature", "").strip().lower()
        return bool(sent) and hmac.compare_digest(sent, gitea_signature(secret, body))
    bearer = headers.get("authorization", "")
    sent = (headers.get("x-webhook-token")
            or (bearer[7:].strip() if bearer.lower().startswith("bearer ") else "")
            or query.get("token", ""))
    return bool(sent) and hmac.compare_digest(sent.encode(), secret.encode())


# --------------------------------------------------------------------------- where from, how often


def parse_networks(text: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Comma- or space-separated CIDRs (a bare address means just that address).
    Raises ValueError naming the first bad entry."""
    nets = []
    for part in re.split(r"[,\s]+", text.strip()):
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            raise ValueError(f"'{part}' isn't an address or network") from None
    return nets


def address_allowed(hook: WebhookEndpoint, ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    try:
        nets = parse_networks(hook.allowed_networks or settings.webhook_networks)
    except ValueError:
        return False
    return any(addr.version == n.version and addr in n for n in nets)


async def over_rate(broker, hook: WebhookEndpoint, now: float | None = None) -> int:
    """0 if this request is within the hook's per-minute limit, else how many requests it
    has had this minute (the first one over is the one worth logging)."""
    import time

    if hook.rate_per_minute <= 0:
        return 0
    minute = int((now or time.time()) // 60)
    key = f"ahq:hook:rate:{hook.id}:{minute}"
    pipe = broker.r.pipeline()
    pipe.incr(key)
    pipe.expire(key, 120)
    count, _ = await pipe.execute()
    return count if count > hook.rate_per_minute else 0


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


def delivery_id(hook: WebhookEndpoint, headers: dict[str, str]) -> str:
    header = (hook.delivery_header or PRESET_DELIVERY_HEADER.get(hook.auth_mode, "")).lower()
    return headers.get(header, "").strip()[:200] if header else ""


def safe_headers(headers: dict[str, str]) -> dict[str, str]:
    """Headers worth keeping in the delivery log: no secrets, nothing huge."""
    return {k: v[:500] for k, v in headers.items() if not _SECRET_HEADER.search(k)}
