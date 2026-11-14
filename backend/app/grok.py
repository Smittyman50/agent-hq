"""Grok plan usage: run agents on your SuperGrok or X Premium+ subscription instead of an API key.

xAI lets third-party agent tools (Hermes Agent, OpenCode, Kilo Code, Warp, ...) sign in to
your xAI account with OAuth and send requests to the regular API at https://api.x.ai/v1,
counted against your subscription instead of billed per token. Agent HQ does the same,
with nothing extra to install:

- Sign-in uses the OAuth device flow (RFC 8628), so it works from the web UI even though
  Agent HQ runs on another machine: the UI shows a short code and a link, you approve on
  xAI's site, and the server picks up the tokens itself. Nothing to paste back.
- xAI only accepts these sign-ins from client IDs it has allowlisted, and doesn't let apps
  register their own. Like the other tools, Agent HQ uses the public client ID of xAI's
  own Grok CLI (a public client, not a secret). Set AHQ_GROK_CLIENT_ID to use another.
- Credentials live in Postgres (on the ModelProvider row, never returned by the API), so
  every worker on every machine uses the same sign-in.
- xAI rotates the refresh token on every refresh, so refreshes are serialized across all
  workers with a Valkey lock: whoever holds it re-reads the stored credentials first and
  adopts them if another worker already refreshed.

Inference goes through the OpenAI-compatible Chat Completions API, with Grok's model
profile so PydanticAI's unified thinking setting becomes xAI's `reasoning_effort`.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx2
import redis.asyncio as aioredis
from pydantic import TypeAdapter
from pydantic_ai.exceptions import UserError
from pydantic_ai.profiles import ModelProfile, merge_profile
from pydantic_ai.profiles.grok import grok_model_profile
from pydantic_ai.profiles.openai import OpenAIJsonSchemaTransformer, OpenAIModelProfile
from pydantic_ai.providers._oauth_provider import (  # the base PydanticAI's ChatGPT and Codex providers use
    OAuthCredentialsProvider,
    post_token_request,
)

from . import db
from .broker import connect
from .config import settings
from .models import ModelProvider

log = logging.getLogger(__name__)

ISSUER = "https://auth.x.ai"
DEVICE_CODE_URL = f"{ISSUER}/oauth2/device/code"
TOKEN_URL = f"{ISSUER}/oauth2/token"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
SCOPE = "openid profile email offline_access grok-cli:access api:access"
GROK_CLI_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"  # xAI's public Grok CLI client
API_BASE = "https://api.x.ai/v1"
# Access tokens last about six hours. Refresh well ahead, so a job that starts just before
# expiry doesn't hit a 401 halfway through a long tool call.
REFRESH_EARLY = 30 * 60
_PENDING = "ahq:grok:signin:"  # + provider id -> pending device sign-in
_NOT_ENTITLED = ("xAI refused this account (HTTP 403). OAuth access is tied to your subscription: "
                 "check that the account you approved with has an active SuperGrok plan, or an "
                 "X Premium+ subscription on a linked X account. Some tiers are refused; use an "
                 "xAI API key with an OpenAI-compatible provider (https://api.x.ai/v1) instead.")
# Models /v1/models lists that agents can't chat with.
_NOT_CHAT = ("imagine", "image", "video", "tts", "voice", "embed", "realtime", "stt")

# Tests swap in an httpx2 transport that fakes xAI.
_transport: Any = None


def client_id() -> str:
    return os.environ.get("AHQ_GROK_CLIENT_ID", "").strip() or GROK_CLI_CLIENT_ID


class NotConnected(UserError):
    pass


# --------------------------------------------------------------------------- credentials

@dataclass(frozen=True)
class GrokCredentials:
    access_token: str
    refresh_token: str
    id_token: str | None = None
    expires_at: float | None = None  # unix time
    scope: str = ""


@dataclass
class _TokenResponse:
    access_token: str
    refresh_token: str | None = None
    id_token: str | None = None
    expires_in: float | None = None
    scope: str | None = None


_token_ta = TypeAdapter(_TokenResponse)
_TOKEN_FIELDS = ("access_token", "refresh_token", "id_token", "expires_at", "scope")


def _from_response(t: _TokenResponse, previous: GrokCredentials | None = None) -> GrokCredentials:
    return GrokCredentials(
        access_token=t.access_token,
        # xAI normally rotates it; keep the old one if a response ever leaves it out
        refresh_token=t.refresh_token or (previous.refresh_token if previous else ""),
        id_token=t.id_token or (previous.id_token if previous else None),
        expires_at=time.time() + t.expires_in if t.expires_in else None,
        scope=t.scope or (previous.scope if previous else ""),
    )


def _claims(jwt: str | None) -> dict:
    if not jwt or jwt.count(".") != 2:
        return {}
    payload = jwt.split(".")[1]
    try:
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError:
        return {}


def status(p: ModelProvider) -> dict:
    """What the UI shows: connected or not, and which account."""
    o = p.oauth or {}
    connected = bool(o.get("refresh_token"))
    claims = _claims(o.get("id_token"))
    return {
        "connected": connected,
        "account": (claims.get("email") or claims.get("name")) if connected else None,
        "connected_at": o.get("connected_at") if connected else None,
    }


def _load(provider_id: str) -> ModelProvider:
    with db.session() as s:
        p = s.get(ModelProvider, provider_id)
    if p is None:
        raise UserError("this Grok provider no longer exists")
    return p


def _to_creds(p: ModelProvider) -> GrokCredentials:
    o = p.oauth or {}
    if not o.get("refresh_token"):
        raise NotConnected(f"'{p.name}' is not signed in to xAI yet. "
                           "Open it on the Models page and choose Sign in with xAI.")
    return GrokCredentials(access_token=o.get("access_token") or "", refresh_token=o["refresh_token"],
                           id_token=o.get("id_token"), expires_at=o.get("expires_at"),
                           scope=o.get("scope") or "")


def _store(provider_id: str, creds: GrokCredentials, *, new_sign_in: bool = False) -> None:
    with db.session() as s:
        p = s.get(ModelProvider, provider_id)
        if p is None:
            raise UserError("this Grok provider no longer exists")
        o = dict(p.oauth or {})
        o.update({f: getattr(creds, f) for f in _TOKEN_FIELDS})
        now = datetime.now(timezone.utc).isoformat()
        o["updated_at"] = now
        if new_sign_in or not o.get("connected_at"):
            o["connected_at"] = now
        p.oauth = o  # reassign so the JSON column is marked dirty
        s.add(p)
        s.commit()


class DbCredentialSource:
    """Credential source backed by the provider's row in the database."""

    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id

    async def load(self) -> GrokCredentials:
        return _to_creds(_load(self.provider_id))

    async def save(self, credentials: GrokCredentials) -> None:
        _store(self.provider_id, credentials)


# --------------------------------------------------------------------------- provider

def model_profile(model_name: str) -> ModelProfile | None:
    """Grok's profile, plus what the OpenAI-compatible request needs to carry its thinking
    setting: the `reasoning_effort` values the model accepts."""
    grok = grok_model_profile(model_name) or {}
    efforts = frozenset(grok.get("grok_reasoning_efforts") or ())
    return merge_profile(
        OpenAIModelProfile(json_schema_transformer=OpenAIJsonSchemaTransformer),
        grok,
        OpenAIModelProfile(openai_supports_reasoning=bool(efforts), openai_reasoning_efforts=efforts),
    )


class GrokPlanProvider(OAuthCredentialsProvider[GrokCredentials]):
    """api.x.ai with your signed-in xAI account's token in place of an API key.

    PydanticAI's OAuth base does the per-process work: it sends the bearer only to
    api.x.ai over HTTPS, refreshes ahead of expiry, and on a 401 refreshes once and
    replays. Agent HQ adds the database credential source and the cross-worker lock.
    """

    def __init__(self, provider_id: str, *, http_client: httpx2.AsyncClient | None = None) -> None:
        self.provider_id = provider_id
        super().__init__(None, credential_source=DbCredentialSource(provider_id), openai_client=None,
                         http_client=http_client)

    @property
    def name(self) -> str:
        return "grok-plan"

    @property
    def canonical_system(self) -> str:
        return "grok-plan"

    @property
    def base_url(self) -> str:
        return API_BASE

    @staticmethod
    def model_profile(model_name: str) -> ModelProfile | None:
        return model_profile(model_name)

    def _auth_headers(self, credentials: GrokCredentials) -> dict[str, str]:
        return {"Authorization": f"Bearer {credentials.access_token}"}

    async def _refresh_grant(self, credentials: GrokCredentials,
                             http_client: httpx2.AsyncClient) -> GrokCredentials:
        form = {"grant_type": "refresh_token", "client_id": client_id(),
                "refresh_token": credentials.refresh_token}
        t = await post_token_request(TOKEN_URL, form, _token_ta, provider_name=self.name,
                                     http_client=http_client,
                                     rejected_grant_errors=frozenset({"invalid_grant", "invalid_token"}))
        return _from_response(t, credentials)

    def _is_stale(self) -> bool:
        c = self.credentials
        if not c.access_token:
            return True
        if c.expires_at:
            return time.time() >= c.expires_at - REFRESH_EARLY
        return super()._is_stale()  # falls back to the token's own JWT expiry

    async def _refresh_locked(self) -> None:  # PydanticAI hook, called holding its own lock
        r = connect(settings.redis_url)
        try:
            async with r.lock(f"ahq:lock:grok-refresh:{self.provider_id}", timeout=60,
                              blocking_timeout=90):
                await super()._refresh_locked()
        finally:
            await r.aclose()


def _http_client() -> httpx2.AsyncClient | None:
    return httpx2.AsyncClient(transport=_transport) if _transport is not None else None


def make_provider(p: ModelProvider) -> GrokPlanProvider:
    _to_creds(p)  # fail early, with a helpful message, when not signed in
    return GrokPlanProvider(p.id, http_client=_http_client())


def build_model(p: ModelProvider, model_name: str):
    from pydantic_ai.models.openai import OpenAIChatModel

    return OpenAIChatModel(model_name, provider=make_provider(p))


async def list_models(p: ModelProvider) -> list[dict]:
    """The chat models your subscription can use (image, video and voice models left out)."""
    provider = make_provider(p)
    try:
        body = await provider.client.get("/models", cast_to=object)
    except Exception as e:
        if getattr(e, "status_code", None) == 403:
            raise UserError(_NOT_ENTITLED) from e
        raise
    finally:
        await provider.client.close()
    if not isinstance(body, dict):
        raise UserError(f"unexpected model list from xAI: {str(body)[:200]}")
    out = []
    for m in body.get("data") or body.get("models") or []:
        mid = m.get("id") if isinstance(m, dict) else None
        if mid and not any(w in mid.lower() for w in _NOT_CHAT):
            out.append({"id": mid, "details": m.get("owned_by") or ""})
    return sorted(out, key=lambda m: m["id"])


# --------------------------------------------------------------------------- sign-in

async def _post_form(url: str, form: dict[str, str]) -> httpx2.Response:
    async with httpx2.AsyncClient(transport=_transport, timeout=httpx2.Timeout(30, connect=5)) as c:
        return await c.post(url, data=form, headers={"Accept": "application/json"})


async def start_sign_in(r: aioredis.Redis, p: ModelProvider) -> dict:
    """Begin a device sign-in. Returns the code to show and where to enter it."""
    resp = await _post_form(DEVICE_CODE_URL, {"client_id": client_id(), "scope": SCOPE})
    if resp.status_code != 200:
        if resp.status_code == 403:
            raise UserError(_NOT_ENTITLED)
        raise UserError(f"xAI wouldn't start a sign-in (HTTP {resp.status_code}): {resp.text[:200]}")
    d = resp.json()
    for k in ("device_code", "user_code", "verification_uri"):
        if not d.get(k):
            raise UserError(f"xAI's sign-in response is missing {k}")
    expires_in = int(d.get("expires_in") or 600)
    interval = max(int(d.get("interval") or 5), 5)
    pending = {"device_code": d["device_code"], "interval": interval, "next_poll": 0}
    await r.set(_PENDING + p.id, json.dumps(pending), ex=expires_in)
    return {"user_code": d["user_code"], "verification_uri": d["verification_uri"],
            "verification_uri_complete": d.get("verification_uri_complete") or d["verification_uri"],
            "expires_in": expires_in, "interval": interval}


async def poll_sign_in(r: aioredis.Redis, provider_id: str) -> dict:
    """Check once whether you've approved. {"state": "pending", "interval"} or {"state": "done"}.

    The UI calls this every few seconds. Calls that come faster than xAI allows are
    answered "pending" without asking xAI, so a busy UI can't trip its rate limit.
    """
    raw = await r.get(_PENDING + provider_id)
    if raw is None:
        raise UserError("this sign-in has expired or was already finished; start it again")
    pending = json.loads(raw)
    now = time.time()
    if now < pending.get("next_poll", 0):
        return {"state": "pending", "interval": pending["interval"]}
    pending["next_poll"] = now + pending["interval"]
    ttl = await r.ttl(_PENDING + provider_id)
    await r.set(_PENDING + provider_id, json.dumps(pending), ex=max(int(ttl), 1))
    resp = await _post_form(TOKEN_URL, {"grant_type": DEVICE_GRANT, "client_id": client_id(),
                                        "device_code": pending["device_code"]})
    if resp.status_code == 200:
        await r.delete(_PENDING + provider_id)
        creds = _from_response(_token_ta.validate_python(resp.json()))
        if not creds.refresh_token:
            raise UserError("xAI signed you in but gave no refresh token, so the sign-in "
                            "couldn't outlive the first access token. Try again.")
        _store(provider_id, creds, new_sign_in=True)
        return {"state": "done"}
    try:
        err = resp.json().get("error") or ""
    except ValueError:
        err = ""
    if err == "authorization_pending":
        return {"state": "pending", "interval": pending["interval"]}
    if err == "slow_down":
        pending["interval"] += 5
        await r.set(_PENDING + provider_id, json.dumps(pending), ex=max(int(ttl), 1))
        return {"state": "pending", "interval": pending["interval"]}
    await r.delete(_PENDING + provider_id)
    if err == "access_denied":
        raise UserError("the sign-in was declined, so nothing was changed")
    if err == "expired_token":
        raise UserError("the code expired before it was approved; start the sign-in again")
    if resp.status_code == 403:
        raise UserError(_NOT_ENTITLED)
    raise UserError(f"xAI refused the sign-in (HTTP {resp.status_code}): {err or resp.text[:200]}")


async def cancel_sign_in(r: aioredis.Redis, provider_id: str) -> None:
    await r.delete(_PENDING + provider_id)


def disconnect(provider_id: str) -> None:
    """Forget the tokens. To revoke access entirely, also remove the app in your xAI account."""
    with db.session() as s:
        p = s.get(ModelProvider, provider_id)
        if p is None or not p.oauth:
            return
        p.oauth = None
        s.add(p)
        s.commit()

