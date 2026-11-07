"""Agent avatars: GIF (animated or not), PNG, JPEG or WebP.

Images live in the database (AgentAvatar), so every machine serves the same ones and
they travel with agent export/import. URLs carry ?v=<avatar_version>, so browsers can
cache them forever and still pick up a new upload immediately.
"""

from __future__ import annotations

import base64
import io

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from PIL import Image, ImageSequence, UnidentifiedImageError
from sqlmodel import Session

from ..config import settings
from ..db import get_session
from ..models import AgentAvatar, AgentDef, _now

router = APIRouter(prefix="/api/agents/{agent_id}/avatar", tags=["avatars"])

FORMATS = {"GIF": "image/gif", "PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
CACHE = {"Cache-Control": "public, max-age=31536000, immutable"}


def inspect_image(data: bytes) -> dict:
    """Validate an upload and make a still (first frame, PNG, at most 256px)."""
    if not data:
        raise HTTPException(422, "empty file")
    if len(data) > settings.avatar_max_bytes:
        raise HTTPException(413, f"avatar is larger than {settings.avatar_max_bytes // 1024} KB")
    try:
        with Image.open(io.BytesIO(data)) as im:
            fmt = im.format
            if fmt not in FORMATS:
                raise HTTPException(415, "use a GIF, PNG, JPEG or WebP image")
            frames = getattr(im, "n_frames", 1)
            width, height = im.size
            first = next(ImageSequence.Iterator(im)).convert("RGBA")
            first.thumbnail((256, 256))
            buf = io.BytesIO()
            first.save(buf, format="PNG")
    except UnidentifiedImageError as e:
        raise HTTPException(415, "that file is not an image") from e
    return {"content_type": FORMATS[fmt], "still": buf.getvalue(), "width": width,
            "height": height, "frames": frames}


def store_avatar(s: Session, agent: AgentDef, data: bytes) -> AgentAvatar:
    info = inspect_image(data)
    row = s.get(AgentAvatar, agent.id) or AgentAvatar(agent_id=agent.id, content_type="", data=b"", still=b"")
    row.data = data
    for k, v in info.items():
        setattr(row, k, v)
    row.updated_at = _now()
    agent.avatar_version += 1
    agent.avatar_animated = info["frames"] > 1
    s.add(row)
    s.add(agent)
    return row


def export_avatar(s: Session, agent_id: str) -> dict | None:
    row = s.get(AgentAvatar, agent_id)
    if not row:
        return None
    return {"content_type": row.content_type, "data_base64": base64.b64encode(row.data).decode()}


def _agent(s: Session, agent_id: str) -> AgentDef:
    a = s.get(AgentDef, agent_id)
    if not a:
        raise HTTPException(404, "agent not found")
    return a


@router.put("", response_model=AgentDef)
async def upload_avatar(agent_id: str, file: UploadFile = File(...), s: Session = Depends(get_session)):
    agent = _agent(s, agent_id)
    data = await file.read(settings.avatar_max_bytes + 1)
    store_avatar(s, agent, data)
    s.commit()
    s.refresh(agent)
    return agent


@router.get("")
def get_avatar(agent_id: str, s: Session = Depends(get_session)):
    row = s.get(AgentAvatar, agent_id)
    if not row:
        raise HTTPException(404)
    return Response(row.data, media_type=row.content_type, headers=CACHE)


@router.get("/still")
def get_still(agent_id: str, s: Session = Depends(get_session)):
    row = s.get(AgentAvatar, agent_id)
    if not row:
        raise HTTPException(404)
    return Response(row.still, media_type="image/png", headers=CACHE)


@router.delete("", response_model=AgentDef)
def delete_avatar(agent_id: str, s: Session = Depends(get_session)):
    agent = _agent(s, agent_id)
    row = s.get(AgentAvatar, agent_id)
    if row:
        s.delete(row)
    agent.avatar_version = 0
    agent.avatar_animated = False
    s.add(agent)
    s.commit()
    s.refresh(agent)
    return agent
