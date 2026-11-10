"""Settings for long conversations: compaction and context windows (app/context.py)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from .. import context

router = APIRouter(tags=["conversations"])


@router.get("/api/conversations/settings")
def get_settings():
    return context.get_settings()


@router.put("/api/conversations/settings")
def put_settings(body: dict[str, Any]):
    try:
        return context.save_settings(body)
    except (TypeError, ValueError) as e:
        raise HTTPException(422, str(e)) from None
