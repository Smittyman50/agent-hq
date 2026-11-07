"""Model providers (Ollama servers, OpenAI, OpenAI-compatible servers, Anthropic).

API keys are write-only: they are stored, but never returned. Responses say only
whether a key is set.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

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
    enabled: bool = True


class ProviderOut(BaseModel):
    id: str
    name: str
    kind: ProviderKind
    base_url: str | None
    has_api_key: bool
    enabled: bool
    created_at: datetime


def out(p: ModelProvider) -> ProviderOut:
    return ProviderOut(id=p.id, name=p.name, kind=p.kind, base_url=p.base_url,
                       has_api_key=bool(p.api_key), enabled=p.enabled, created_at=p.created_at)


def _validate(s: Session, body: ProviderIn, own_id: str | None = None) -> None:
    if not body.name.strip():
        raise HTTPException(422, "name is required")
    if body.kind == ProviderKind.openai_compatible and not (body.base_url or "").strip():
        raise HTTPException(422, "an OpenAI-compatible provider needs a base URL")
    other = s.exec(select(ModelProvider).where(ModelProvider.name == body.name)).first()
    if other and other.id != own_id:
        raise HTTPException(409, f"a provider named '{body.name}' already exists")


def _apply(p: ModelProvider, body: ProviderIn) -> None:
    p.name, p.kind, p.enabled = body.name.strip(), body.kind, body.enabled
    p.base_url = (body.base_url or "").strip() or None
    if body.api_key is not None:
        p.api_key = body.api_key.strip() or None


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
    users = s.exec(select(AgentDef).where(AgentDef.provider_id == provider_id)).all()
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
