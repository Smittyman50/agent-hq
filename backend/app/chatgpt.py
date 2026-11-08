"""ChatGPT plan usage: run agents on your ChatGPT Plus/Pro plan instead of an API key.

Uses OpenAI's "Sign in with ChatGPT" flow for open-source and self-hosted apps
(https://developers.openai.com/siwc/token-sharing-open-source), through PydanticAI's
OpenAIChatGPTProvider. What Agent HQ adds on top:

- Sign-in from the web UI, even though Agent HQ runs on another machine. OpenAI only
  redirects to http://127.0.0.1:<port>/auth/callback, which is the computer running your
  browser, not the server. So the UI opens the sign-in page, and after you approve, you
  paste the address your browser ended up on (it shows a "can't connect" page) back
  into Agent HQ. The server holds the PKCE verifier and does the code exchange itself.
- Credentials live in Postgres (on the ModelProvider row, never returned by the API),
  so every worker on every machine uses the same sign-in.
- Refresh tokens are single-use, so refreshes are serialized across all workers with a
  Valkey lock: whoever holds it re-reads the stored credentials first and adopts them if
  another worker already refreshed.
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlsplit

import redis.asyncio as aioredis
from pydantic_ai.exceptions import UserError
from pydantic_ai.providers.openai_chatgpt import (
    OpenAIChatGPTCredentials,
    OpenAIChatGPTOAuthFlow,
    OpenAIChatGPTProvider,
)

from . import db
from .broker import connect
from .config import settings
from .models import ModelProvider, SystemSetting

log = logging.getLogger(__name__)

APP_NAME = "Agent HQ"  # shown to you on OpenAI's approval page; keep it stable
REDIRECT_URI = "http://127.0.0.1:1455/auth/callback"
SIGNIN_TTL = 15 * 60  # seconds a started sign-in stays valid
_PENDING = "ahq:chatgpt:signin:"  # + state -> pending sign-in (PKCE verifier, nonce, ...)
_TOKEN_FIELDS = ("access_token", "refresh_token", "id_token", "scope")

# Tests swap in an httpx2 transport that fakes OpenAI.
_transport: Any = None


class NotConnected(UserError):
    pass


# --------------------------------------------------------------------------- identity

def host_id() -> str:
    """This Agent HQ installation's stable host ID (OpenAI's `ext_agent_host_id`)."""
    with db.session() as s:
        row = s.get(SystemSetting, "chatgpt_host_id")
        if row is None:
            row = SystemSetting(key="chatgpt_host_id", value={"id": uuid.uuid4().urn})
            s.add(row)
            s.commit()
            s.refresh(row)
        return row.value["id"]


def _claims(jwt: str | None) -> dict:
    if not jwt or jwt.count(".") != 2:
        return {}
    payload = jwt.split(".")[1]
    try:
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError:
        return {}


def status(p: ModelProvider) -> dict:
    """What the UI shows: connected or not, which account, whether plan usage was granted."""
    o = p.oauth or {}
    connected = bool(o.get("refresh_token"))
    claims = _claims(o.get("id_token"))
    return {
        "connected": connected,
        "account": (claims.get("email") or claims.get("name")) if connected else None,
        "plan_usage": connected and "chatgpt.tokens.use.direct" in (o.get("scope") or "").split(),
        "connected_at": o.get("connected_at") if connected else None,
    }


# --------------------------------------------------------------------------- credentials

def _load(provider_id: str) -> ModelProvider:
    with db.session() as s:
        p = s.get(ModelProvider, provider_id)
    if p is None:
        raise UserError("this ChatGPT provider no longer exists")
    return p


def _to_creds(p: ModelProvider) -> OpenAIChatGPTCredentials:
    o = p.oauth or {}
    if not o.get("refresh_token"):
        raise NotConnected(f"'{p.name}' is not signed in to ChatGPT yet. "
                           "Open it on the Models page and choose Sign in with ChatGPT.")
    return OpenAIChatGPTCredentials(client_id=o["client_id"], access_token=o["access_token"],
                                    refresh_token=o["refresh_token"], id_token=o.get("id_token"),
                                    scope=o.get("scope") or "")


def _store(provider_id: str, creds: OpenAIChatGPTCredentials, *, new_sign_in: bool = False) -> None:
    with db.session() as s:
        p = s.get(ModelProvider, provider_id)
        if p is None:
            raise UserError("this ChatGPT provider no longer exists")
        o = dict(p.oauth or {})
        o.update(client_id=creds.client_id, **{f: getattr(creds, f) for f in _TOKEN_FIELDS})
        now = datetime.now(timezone.utc).isoformat()
        o["updated_at"] = now
        if new_sign_in or not o.get("connected_at"):
            o["connected_at"] = now
        p.oauth = o  # reassign so the JSON column is marked dirty
        s.add(p)
        s.commit()


class DbCredentialSource:
    """OpenAIChatGPTCredentialSource backed by the provider's row in the database."""

    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id

    async def load(self) -> OpenAIChatGPTCredentials:
        return _to_creds(_load(self.provider_id))

    async def save(self, credentials: OpenAIChatGPTCredentials) -> None:
        _store(self.provider_id, credentials)


class AgentHQChatGPTProvider(OpenAIChatGPTProvider):
    """PydanticAI's provider, with refreshes serialized across every worker via Valkey.

    PydanticAI already single-flights refreshes inside one process and re-reads storage
    before spending a refresh token; the lock makes that re-read-then-refresh atomic
    across processes and machines, so two workers never spend the same refresh token.
    """

    def __init__(self, provider_id: str, **kwargs: Any) -> None:
        super().__init__(credential_source=DbCredentialSource(provider_id), **kwargs)
        self.provider_id = provider_id

    async def _refresh_locked(self) -> None:  # PydanticAI hook, called holding its own lock
        r = connect(settings.redis_url)
        try:
            async with r.lock(f"ahq:lock:chatgpt-refresh:{self.provider_id}", timeout=60,
                              blocking_timeout=90):
                await super()._refresh_locked()
        finally:
            await r.aclose()


def make_provider(p: ModelProvider) -> AgentHQChatGPTProvider:
    kwargs: dict[str, Any] = {}
    if _transport is not None:
        import httpx2

        kwargs["http_client"] = httpx2.AsyncClient(transport=_transport)
    _to_creds(p)  # fail early, with a helpful message, when not signed in
    return AgentHQChatGPTProvider(p.id, **kwargs)


def build_model(p: ModelProvider, model_name: str):
    from pydantic_ai.models.openai import OpenAIResponsesModel

    return OpenAIResponsesModel(model_name, provider=make_provider(p))


async def list_models(p: ModelProvider) -> list[dict]:
    """The models this ChatGPT plan offers, in OpenAI's order (only those meant for pickers).

    Plan usage answers GET /v1/models with {"models": [{"slug", "display_name", "visibility"}]},
    not the regular API's {"data": [{"id"}]}. The slug is what goes in a request's `model`.
    """
    provider = make_provider(p)
    try:
        body = await provider.client.get("/models", cast_to=object)
    finally:
        await provider.client.close()
    if not isinstance(body, dict):
        raise UserError(f"unexpected model list from OpenAI: {str(body)[:200]}")
    entries = body.get("models")
    if entries is None:
        entries = body.get("data") or []  # the regular API's shape, just in case
    out = []
    for m in entries:
        if not isinstance(m, dict) or m.get("visibility", "list") != "list":
            continue
        slug = m.get("slug") or m.get("id")
        if slug:
            out.append({"id": slug, "details": m.get("display_name") or ""})
    return out


# --------------------------------------------------------------------------- sign-in

async def start_sign_in(r: aioredis.Redis, p: ModelProvider) -> str:
    """Begin a sign-in for this provider. Returns the URL to open in the browser."""
    o = p.oauth or {}
    client_id = o.get("client_id")  # reuse the registration from an earlier sign-in
    hint = o.get("id_token") if client_id else None
    flow = OpenAIChatGPTOAuthFlow(app_name=APP_NAME, host_id=host_id(), client_id=client_id,
                                  id_token_hint=hint, redirect_uri=REDIRECT_URI)
    extra = None
    if o.get("refresh_token") and "chatgpt.tokens.use.direct" not in (o.get("scope") or "").split():
        extra = {"prompt": "consent"}  # you declined plan usage last time: ask again
    url = flow.authorization_url(extra_params=extra)
    pending = {"provider_id": p.id, "state": flow.state, "code_verifier": flow.code_verifier,
               "nonce": flow.nonce, "client_id": client_id, "id_token_hint": hint}
    await r.set(_PENDING + flow.state, json.dumps(pending), ex=SIGNIN_TTL)
    return url


def _parse_callback(text: str) -> dict[str, str]:
    text = text.strip()
    if not text:
        raise UserError("paste the address from your browser's address bar")
    query = urlsplit(text).query if "://" in text else text.lstrip("?")
    params = {k: v[0] for k, v in parse_qs(query).items() if v}
    if not params.get("state"):
        raise UserError("that address has no sign-in result in it. Copy the whole address "
                        "(starting with http://127.0.0.1:1455/auth/callback?...) after approving.")
    return params


async def finish_sign_in(r: aioredis.Redis, provider_id: str, callback: str) -> dict:
    """Complete a sign-in from the pasted callback address. Returns the new status."""
    params = _parse_callback(callback)
    raw = await r.get(_PENDING + params["state"])
    if raw is None:
        raise UserError("this sign-in has expired or was already used; start it again")
    pending = json.loads(raw)
    if pending["provider_id"] != provider_id:
        raise UserError("that sign-in was started for a different provider")
    await r.delete(_PENDING + params["state"])  # one use only
    if params.get("error"):
        if params["error"] == "access_denied":
            raise UserError("the sign-in was declined, so nothing was changed")
        raise UserError(f"OpenAI reported an error: {params.get('error_description') or params['error']}")
    if not params.get("code"):
        raise UserError("that address has no authorization code in it")
    flow = OpenAIChatGPTOAuthFlow(app_name=APP_NAME, host_id=host_id(),
                                  client_id=pending["client_id"],
                                  id_token_hint=pending["id_token_hint"],
                                  redirect_uri=REDIRECT_URI, state=pending["state"])
    flow.code_verifier = pending["code_verifier"]
    flow.nonce = pending["nonce"]
    creds = await flow.exchange_code(params["code"], client_id=params.get("client_id"))
    _store(provider_id, creds, new_sign_in=True)
    p = _load(provider_id)
    st = status(p)
    if not st["plan_usage"]:
        raise UserError("you signed in, but didn't allow Agent HQ to use your ChatGPT plan. "
                        "Sign in again and approve plan usage.")
    return st


def disconnect(provider_id: str) -> None:
    """Forget the tokens. The client registration is kept so a later sign-in reuses it.

    To revoke access entirely, also disconnect Agent HQ in your ChatGPT settings.
    """
    with db.session() as s:
        p = s.get(ModelProvider, provider_id)
        if p is None or not p.oauth:
            return
        p.oauth = {"client_id": p.oauth.get("client_id")} if p.oauth.get("client_id") else None
        s.add(p)
        s.commit()
