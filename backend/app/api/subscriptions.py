"""MQTT event subscriptions: which topics wake which agent, and where it reports."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session, select

from ..db import get_session
from ..models import AgentDef, Subscription, Thread

router = APIRouter(prefix="/api/subscriptions", tags=["events"])


class SubscriptionIn(BaseModel):
    name: str
    topic_filter: str
    agent_id: str
    thread_id: str | None = None
    prompt_template: str = ""
    min_interval_seconds: int = 60
    enabled: bool = True


def _validate(s: Session, body: SubscriptionIn, own_id: str | None = None) -> None:
    if not body.name.strip() or not body.topic_filter.strip():
        raise HTTPException(422, "name and topic filter are required")
    parts = body.topic_filter.split("/")
    if any("#" in p and p != "#" for p in parts) or ("#" in parts and parts[-1] != "#"):
        raise HTTPException(422, "'#' must be a whole, final topic level")
    if not s.get(AgentDef, body.agent_id):
        raise HTTPException(422, "agent not found")
    if body.thread_id and not s.get(Thread, body.thread_id):
        raise HTTPException(422, "conversation not found")
    other = s.exec(select(Subscription).where(Subscription.name == body.name)).first()
    if other and other.id != own_id:
        raise HTTPException(409, f"a subscription named '{body.name}' already exists")


@router.get("", response_model=list[Subscription])
def list_subscriptions(s: Session = Depends(get_session)):
    return s.exec(select(Subscription).order_by(Subscription.name)).all()


@router.post("", response_model=Subscription, status_code=201)
def create_subscription(body: SubscriptionIn, s: Session = Depends(get_session)):
    _validate(s, body)
    sub = Subscription(**body.model_dump())
    s.add(sub)
    s.commit()
    s.refresh(sub)
    return sub


@router.put("/{sub_id}", response_model=Subscription)
def update_subscription(sub_id: str, body: SubscriptionIn, s: Session = Depends(get_session)):
    sub = s.get(Subscription, sub_id)
    if not sub:
        raise HTTPException(404)
    _validate(s, body, sub_id)
    for k, v in body.model_dump().items():
        setattr(sub, k, v)
    s.add(sub)
    s.commit()
    s.refresh(sub)
    return sub


@router.delete("/{sub_id}", status_code=204)
def delete_subscription(sub_id: str, s: Session = Depends(get_session)):
    sub = s.get(Subscription, sub_id)
    if not sub:
        raise HTTPException(404)
    s.delete(sub)
    s.commit()


class TestEvent(BaseModel):
    topic: str
    payload: str


@router.post("/test")
async def test_event(body: TestEvent, request: Request):
    """Inject a message as if it came from MQTT (cooldowns apply)."""
    from ..mqtt_bridge import route_event

    outcomes = await route_event(request.app.state.runtime, body.topic, body.payload.encode())
    return {"queued_jobs": [o["job_id"] for o in outcomes if o["job_id"]], "outcomes": outcomes}


class PreviewIn(BaseModel):
    prompt_template: str = ""
    topic: str
    payload: str
    source: str = "mqtt"


@router.post("/preview")
def preview(body: PreviewIn):
    """What a template turns into for this event, without queuing anything."""
    from ..mqtt_bridge import parse_json, prompt_payload, render_full

    source = "webhook" if body.source == "webhook" or body.topic.startswith("webhook/") else "mqtt"
    raw = body.payload.encode()
    ctx = None
    if source == "webhook":
        parts = body.topic.split("/")
        ctx = {"source": parts[1] if len(parts) > 1 else "", "event": parts[-1]}
    sub = Subscription(name="preview", topic_filter="#", agent_id="", prompt_template=body.prompt_template)
    prompt, missing = render_full(sub, body.topic, prompt_payload(raw, source), source, ctx, parse_json(raw))
    return {"prompt": prompt, "missing": missing}
