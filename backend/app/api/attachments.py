"""Chat attachments: upload before sending, list, download, and remove unsent ones."""

from __future__ import annotations

import asyncio
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, Response, UploadFile

from .. import attachments
from ..config import settings
from ..db import session
from ..models import Attachment, Thread

router = APIRouter(tags=["attachments"])


@router.post("/api/threads/{thread_id}/attachments", status_code=201)
async def upload(thread_id: str, files: list[UploadFile] = File(...)):
    """Store files for the next message. Send them with the message's attachment_ids."""
    with session() as s:
        t = s.get(Thread, thread_id)
        if t is None or t.deleted_at is not None:
            raise HTTPException(404, "conversation not found")
    if len(files) > settings.attachment_max_files:
        raise HTTPException(422, f"at most {settings.attachment_max_files} files at a time")
    loop = asyncio.get_running_loop()
    out = []
    for f in files:
        data = await f.read(settings.attachment_max_bytes + 1)
        try:
            row = await loop.run_in_executor(None, lambda: attachments.store(
                thread_id, f.filename or "file", data, mime=f.content_type))
        except attachments.AttachmentError as e:
            raise HTTPException(413 if "larger" in str(e) else 422, str(e)) from None
        out.append(attachments.summary(row))
    return out


@router.get("/api/threads/{thread_id}/attachments")
def list_for_thread(thread_id: str):
    return [attachments.summary(a) for a in attachments.for_thread(thread_id)]


def _row(att_id: str) -> Attachment:
    a = attachments.get(att_id)
    if a is None:
        raise HTTPException(404, "file not found")
    return a


@router.get("/api/attachments/{att_id}")
def download(att_id: str, download: bool = False):
    """The file. Images open in the browser; everything else downloads.

    Served with a sandboxing CSP and nosniff, so an uploaded HTML or SVG file can never
    run as a page on Agent HQ's own origin.
    """
    a = _row(att_id)
    inline = a.kind == "image" and not download
    disposition = (f"{'inline' if inline else 'attachment'}; "
                   f"filename*=UTF-8''{quote(a.name)}")
    return Response(a.data, media_type=a.mime if inline else (a.mime or "application/octet-stream"),
                    headers={"Content-Disposition": disposition,
                             "Content-Security-Policy": "sandbox; default-src 'none'",
                             "X-Content-Type-Options": "nosniff",
                             "Cache-Control": "private, max-age=86400"})


@router.get("/api/attachments/{att_id}/text")
def text(att_id: str, offset: int = 0, max_chars: int = attachments.MAX_READ_CHARS):
    return attachments.read_text(_row(att_id), offset, max_chars)


@router.delete("/api/attachments/{att_id}", status_code=204)
def remove(att_id: str):
    """Remove a file you attached but haven't sent yet."""
    a = _row(att_id)
    if a.message_id is not None or a.source != "user":
        raise HTTPException(409, "this file is already part of the conversation")
    with session() as s:
        row = s.get(Attachment, a.id)
        if row is not None:
            s.delete(row)
            s.commit()
