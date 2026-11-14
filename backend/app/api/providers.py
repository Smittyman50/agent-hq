"""Model providers (Ollama servers, OpenAI, OpenAI-compatible servers, Anthropic, and
your ChatGPT or Grok plan).

API keys and plan sign-in tokens are write-only: they are stored, but never returned.
Responses say only whether a key is set, or which account is signed in.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from pydantic_ai.exceptions import UserError
from sqlmodel import Session, select

from .. import chatgpt, grok
from ..db import get_session
from ..llm import list_models
from ..models import AgentDef, ModelProvider, ProviderKind

router = APIRouter(prefix="/api/providers", tags=["models"])


class ProviderIn(BaseModel):
    name: str
    kind: ProviderKind = ProviderKind.ollama
    base_url: str | None = None
    # None = keep the stored key; "" = clear it; anything else = replace it.
    api_key: str | None = None
    # Slots: most model requests at once across all workers (0 = no limit). None = keep.
    max_concurrent: int | None = None
    enabled: bool = True


class ProviderOut(BaseModel):
    id: str
    name: str
    kind: ProviderKind
    base_url: str | None
    has_api_key: bool
    max_concurrent: int = 0
    enabled: bool
    created_at: datetime
    # ChatGPT plan providers only: {connected, account, plan_usage, connected_at}
    chatgpt: dict | None = None
    # Grok plan providers only: {connected, account, connected_at}
    grok: dict | None = None


def out(p: ModelProvider) -> ProviderOut:
    return ProviderOut(id=p.id, name=p.name, kind=p.kind, base_url=p.base_url,
                       has_api_key=bool(p.api_key), max_concurrent=p.max_concurrent or 0, enabled=p.enabled, created_at=p.created_at,
                       chatgpt=chatgpt.status(p) if p.kind == ProviderKind.chatgpt else None,
                       grok=grok.status(p) if p.kind == ProviderKind.grok else None)


def _validate(s: Session, body: ProviderIn, own_id: str | None = None) -> None:
    if not body.name.strip():
        raise HTTPException(422, "name is required")
    if body.max_concurrent is not None and body.max_concurrent < 0:
        raise HTTPException(422, "max concurrent requests can't be negative (0 = no limit)")
    if body.kind == ProviderKind.openai_compatible and not (body.base_url or "").strip():
        raise HTTPException(422, "an OpenAI-compatible provider needs a base URL")
    other = s.exec(select(ModelProvider).where(ModelProvider.name == body.name)).first()
    if other and other.id != own_id:
        raise HTTPException(409, f"a provider named '{body.name}' already exists")


_SIGN_IN_KINDS = (ProviderKind.chatgpt, ProviderKind.grok)


def _apply(p: ModelProvider, body: ProviderIn) -> None:
    previous_kind = p.kind
    p.name, p.kind, p.enabled = body.name.strip(), body.kind, body.enabled
    p.base_url = (body.base_url or "").strip() or None
    if body.api_key is not None:
        p.api_key = body.api_key.strip() or None
    if body.max_concurrent is not None:
        p.max_concurrent = max(0, body.max_concurrent)
    if p.kind in _SIGN_IN_KINDS:
        p.base_url, p.api_key = None, None  # signs in instead; fixed endpoint
    if p.oauth and (p.kind not in _SIGN_IN_KINDS or p.kind != previous_kind):
        p.oauth = None  # a different kind of provider now: its sign-in no longer applies


@router.get("", response_model=list[ProviderOut])
def list_providers(s: Session = Depends(get_session)):
    return [out(p) for p in s.exec(select(ModelProvider).order_by(ModelProvider.name)).all()]


@router.post("", response_model=ProviderOut, status_code=201)
def create_provider(body: ProviderIn, s: Session = Depends(get_session)):
    _validate(s, body)
    p = ModelProvider(name=body.name)
    _apply(p, body)
    s.add(p)
    s.commit()
    s.refresh(p)
    return out(p)


@router.put("/{provider_id}", response_model=ProviderOut)
def update_provider(provider_id: str, body: ProviderIn, s: Session = Depends(get_session)):
    p = s.get(ModelProvider, provider_id)
    if not p:
        raise HTTPException(404)
    _validate(s, body, provider_id)
    _apply(p, body)
    s.add(p)
    s.commit()
    s.refresh(p)
    return out(p)


@router.delete("/{provider_id}", status_code=204)
def delete_provider(provider_id: str, s: Session = Depends(get_session)):
    p = s.get(ModelProvider, provider_id)
    if not p:
        raise HTTPException(404)
    users = [a for a in s.exec(select(AgentDef)).all()
             if provider_id in (a.provider_id, a.vision_provider_id, a.fallback_provider_id)]
    if users:
        names = ", ".join(a.name for a in users)
        raise HTTPException(409, f"still used by: {names}. Move those agents to another provider first.")
    s.delete(p)
    s.commit()


@router.get("/{provider_id}/models")
async def provider_models(provider_id: str, s: Session = Depends(get_session)):
    """The models this provider offers right now (the UI's model picker and 'Test' button)."""
    p = s.get(ModelProvider, provider_id)
    if not p:
        raise HTTPException(404)
    try:
        return await list_models(p)
    except Exception as e:
        raise HTTPException(502, f"could not list models from {p.name}: {e}") from e


# ------------------------------------------------------------------ Sign in with ChatGPT

class ChatGPTFinish(BaseModel):
    # The full address your browser ended up on after approving
    # (http://127.0.0.1:1455/auth/callback?code=...&state=...).
    callback_url: str


def _chatgpt_provider(s: Session, provider_id: str) -> ModelProvider:
    p = s.get(ModelProvider, provider_id)
    if not p:
        raise HTTPException(404)
    if p.kind != ProviderKind.chatgpt:
        raise HTTPException(409, "this provider is not a ChatGPT plan provider")
    return p


@router.post("/{provider_id}/chatgpt/start")
async def chatgpt_start(provider_id: str, request: Request, s: Session = Depends(get_session)):
    """Start a Sign in with ChatGPT. Returns the URL to open in your browser."""
    p = _chatgpt_provider(s, provider_id)
    url = await chatgpt.start_sign_in(request.app.state.broker.r, p)
    return {"url": url, "redirect_uri": chatgpt.REDIRECT_URI, "expires_in": chatgpt.SIGNIN_TTL}


@router.post("/{provider_id}/chatgpt/finish", response_model=ProviderOut)
async def chatgpt_finish(provider_id: str, body: ChatGPTFinish, request: Request,
                         s: Session = Depends(get_session)):
    """Finish it with the address your browser was sent to after you approved."""
    _chatgpt_provider(s, provider_id)
    try:
        await chatgpt.finish_sign_in(request.app.state.broker.r, provider_id, body.callback_url)
    except UserError as e:
        raise HTTPException(422, str(e)) from e
    except Exception as e:  # token endpoint refused, network trouble, ...
        raise HTTPException(502, f"sign-in failed: {e}") from e
    s.expire_all()
    return out(s.get(ModelProvider, provider_id))


@router.post("/{provider_id}/chatgpt/disconnect", response_model=ProviderOut)
def chatgpt_disconnect(provider_id: str, s: Session = Depends(get_session)):
    _chatgpt_provider(s, provider_id)
    chatgpt.disconnect(provider_id)
    s.expire_all()
    return out(s.get(ModelProvider, provider_id))


# ------------------------------------------------------------------ Sign in with xAI (Grok plan)

def _grok_provider(s: Session, provider_id: str) -> ModelProvider:
    p = s.get(ModelProvider, provider_id)
    if not p:
        raise HTTPException(404)
    if p.kind != ProviderKind.grok:
        raise HTTPException(409, "this provider is not a Grok plan provider")
    return p


@router.post("/{provider_id}/grok/start")
async def grok_start(provider_id: str, request: Request, s: Session = Depends(get_session)):
    """Start a device sign-in. Returns the code to enter on xAI's site, and where."""
    p = _grok_provider(s, provider_id)
    try:
        return await grok.start_sign_in(request.app.state.broker.r, p)
    except UserError as e:
        raise HTTPException(422, str(e)) from e
    except Exception as e:  # xAI unreachable, ...
        raise HTTPException(502, f"could not start the sign-in: {e}") from e


@router.post("/{provider_id}/grok/poll")
async def grok_poll(provider_id: str, request: Request, s: Session = Depends(get_session)):
    """Whether you've approved yet. {"state": "pending", "interval"} or the signed-in provider."""
    _grok_provider(s, provider_id)
    try:
        result = await grok.poll_sign_in(request.app.state.broker.r, provider_id)
    except UserError as e:
        raise HTTPException(422, str(e)) from e
    except Exception as e:
        raise HTTPException(502, f"sign-in failed: {e}") from e
    if result["state"] != "done":
        return result
    s.expire_all()
    return {"state": "done", "provider": out(s.get(ModelProvider, provider_id))}


@router.post("/{provider_id}/grok/cancel", status_code=204)
async def grok_cancel(provider_id: str, request: Request, s: Session = Depends(get_session)):
    _grok_provider(s, provider_id)
    await grok.cancel_sign_in(request.app.state.broker.r, provider_id)


@router.post("/{provider_id}/grok/disconnect", response_model=ProviderOut)
def grok_disconnect(provider_id: str, s: Session = Depends(get_session)):
    _grok_provider(s, provider_id)
    grok.disconnect(provider_id)
    s.expire_all()
    return out(s.get(ModelProvider, provider_id))
