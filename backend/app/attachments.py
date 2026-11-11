"""Chat attachments: files you attach to a message, and files agents send back.

* You attach files in the chat (button, drag and drop, or paste). They are stored in the
  database with their extracted text, then linked to your message when you send it.
* The agent's turn starts with a short manifest of the files (name, kind, size, ID and,
  for documents, the first lines). It reads them with `read_attachment`, asks its vision
  model about images with `look_at_attachment`, copies them to its workbench with
  `attachment_to_workbench`, and passes them on with `delegate(..., attachment_ids=[...])`.
* Agents whose own model is multimodal can be set to see images directly
  (AgentDef.sees_images): the images then go into the turn itself. They are taken out of
  the saved history afterwards, so the checkpoint doesn't grow by megabytes per picture.
* Agents send files back with `create_file` (text they write) and `send_from_workbench`
  (a file from their workbench, e.g. a chart a script made). Files made during a turn
  appear on the agent's reply; files made by a delegate appear on its delegation card
  and are listed in its result, so the delegator can pick them up.

Every agent working in a conversation can read every file in it, which is what lets a
supervisor pass your PDF on to a teammate: delegates work in the same conversation.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import mimetypes
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from PIL import Image, UnidentifiedImageError
from pydantic_ai import BinaryContent
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart, UserPromptPart
from sqlmodel import select

from . import db
from .config import settings
from .models import Attachment, Message

log = logging.getLogger("agent_hq.attachments")

IMAGE_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "GIF": "image/gif",
                 "WEBP": "image/webp", "BMP": "image/bmp"}
# The longest side an image is sent to a model at (bigger costs tokens, adds little).
MODEL_IMAGE_SIDE = 1568
PREVIEW_CHARS = 1200
READ_CHARS = 8000
MAX_READ_CHARS = 30000

ATTACHMENT_RULES = """\
Files in this conversation:
- Files the user attached are listed with your task, each with an ID like att_ab12cd34ef56.
  Read documents and text with `read_attachment` (in pages; follow next_offset). Ask about
  images with `look_at_attachment`. Never guess at a file's contents you haven't read.
- To work on a file with code, copy it to your workbench with `attachment_to_workbench`.
- When you delegate work that needs a file, pass its ID: delegate(..., attachment_ids=[...]).
- To give the user a file, use `create_file` for text you write (Markdown, CSV, JSON, code)
  or `send_from_workbench` for a file on your workbench. It appears with your reply; say
  what it is, but don't paste its whole content into the reply as well."""

NO_FILES_RULES = """\
Files: to give the user a file (a report, CSV, JSON, config, script), use `create_file`
(or `send_from_workbench` for a file on your workbench). It appears with your reply; say
what it is, but don't paste its whole content into the reply as well."""


class AttachmentError(ValueError):
    """A file that can't be stored (too big, empty, ...). The message is user-facing."""


# --------------------------------------------------------------------------- storing


def _safe_name(name: str) -> str:
    name = PurePosixPath((name or "file").replace("\\", "/")).name.strip() or "file"
    name = re.sub(r"[\x00-\x1f\x7f/]", "_", name)
    return name[:200]


def _image_info(data: bytes) -> tuple[str, int, int] | None:
    """(mime, width, height) when the bytes are an image we can show and send to models."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.format not in IMAGE_FORMATS:
                return None
            return IMAGE_FORMATS[im.format], im.size[0], im.size[1]
    except (UnidentifiedImageError, OSError, ValueError):
        return None


def _extract(name: str, data: bytes) -> tuple[str | None, str, str]:
    """(text, error, kind) for a non-image file."""
    from .knowledge import CONVERT_EXT, TEXT_EXT, to_markdown

    ext = PurePosixPath(name.lower()).suffix
    code_like = ext in {".py", ".js", ".ts", ".sh", ".ps1", ".sql", ".xml", ".ini", ".toml",
                        ".conf", ".cfg", ".env", ".html", ".htm", ".css", ".yaml", ".yml"}
    if ext in CONVERT_EXT:
        try:
            return to_markdown(name, data), "", "document"
        except Exception as e:  # a damaged PDF must not stop the upload
            log.info("attachment %s: no text (%s)", name, e)
            return None, f"couldn't read the text: {e}"[:300], "document"
    if ext in TEXT_EXT or code_like or not ext or _looks_like_text(data):
        if b"\x00" in data[:4096]:
            return None, "binary file", "file"
        for enc in ("utf-8", "utf-16", "latin-1"):
            try:
                return data.decode(enc), "", "text"
            except UnicodeDecodeError:
                continue
    return None, "no text to read in this kind of file", "file"


def _looks_like_text(data: bytes) -> bool:
    head = data[:4096]
    if not head or b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def store(thread_id: str, name: str, data: bytes, *, source: str = "user",
          agent_id: str | None = None, job_id: str | None = None,
          message_id: str | None = None, mime: str | None = None) -> Attachment:
    """Validate, extract text and save one file. Raises AttachmentError."""
    if not data:
        raise AttachmentError(f"{name or 'file'} is empty")
    if len(data) > settings.attachment_max_bytes:
        raise AttachmentError(f"{name} is larger than {settings.attachment_max_bytes // (1024 * 1024)} MB")
    name = _safe_name(name)
    img = _image_info(data)
    if img:
        mime, width, height = img
        text, error, kind = None, "", "image"
    else:
        width = height = 0
        text, error, kind = _extract(name, data)
        guessed = mimetypes.guess_type(name)[0]
        mime = mime if mime and mime != "application/octet-stream" else guessed
        if not mime or mime.startswith("image/"):  # an "image" PIL can't open (SVG, HEIC)
            mime = guessed if guessed and not guessed.startswith("image/") else "application/octet-stream"
            if kind == "text":
                mime = "text/plain"
    row = Attachment(thread_id=thread_id, message_id=message_id, source=source, agent_id=agent_id,
                     job_id=job_id, name=name, mime=mime, kind=kind, size=len(data),
                     sha256=hashlib.sha256(data).hexdigest(), width=width, height=height,
                     data=data, text=text, text_error=error)
    with db.session() as s:
        s.add(row)
        s.commit()
        s.refresh(row)
    return row


# --------------------------------------------------------------------------- reading


def ref(att_id: str) -> str:
    return f"att_{att_id}"


def unref(value: str) -> str:
    v = (value or "").strip().strip("`'\"")
    return v[4:] if v.startswith("att_") else v


def get(att_id: str) -> Attachment | None:
    with db.session() as s:
        return s.get(Attachment, unref(att_id))


def in_thread(thread_id: str, ids: list[str]) -> list[Attachment]:
    """The given files, if all of them belong to this conversation (else AttachmentError)."""
    out = []
    for i in dict.fromkeys(unref(x) for x in ids or []):
        a = get(i)
        if a is None or a.thread_id != thread_id:
            raise AttachmentError(f"{ref(i)} is not a file in this conversation")
        out.append(a)
    return out


def for_thread(thread_id: str) -> list[Attachment]:
    with db.session() as s:
        return list(s.exec(select(Attachment).where(Attachment.thread_id == thread_id)
                           .order_by(Attachment.created_at)).all())


def has_any(thread_id: str) -> bool:
    with db.session() as s:
        return s.exec(select(Attachment.id).where(Attachment.thread_id == thread_id)).first() is not None


def summary(a: Attachment) -> dict[str, Any]:
    """What the UI and message metadata carry (never the bytes)."""
    return {"id": a.id, "ref": ref(a.id), "name": a.name, "mime": a.mime, "kind": a.kind,
            "size": a.size, "width": a.width, "height": a.height, "source": a.source,
            "agent_id": a.agent_id, "has_text": a.text is not None,
            "message_id": a.message_id, "created_at": a.created_at.isoformat() if a.created_at else None}


def human_size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def manifest(atts: list[Attachment], *, preview: bool = True, sees_images: bool = False,
             heading: str = "Attached files") -> str:
    """The block added to a turn or task that tells the agent what it was given."""
    if not atts:
        return ""
    lines = [f"[{heading}]"]
    for a in atts:
        desc = f"- {ref(a.id)}: {a.name} ({a.kind}, {human_size(a.size)}"
        if a.kind == "image":
            desc += f", {a.width}x{a.height}"
        desc += ")"
        if a.kind == "image":
            desc += (" (shown to you with this message)" if sees_images
                     else " (ask about it with look_at_attachment)")
        elif a.text is not None:
            words = len(a.text.split())
            desc += f", {words} words of text"
            if preview and a.text.strip():
                head = a.text.strip()[:PREVIEW_CHARS]
                more = len(a.text.strip()) > PREVIEW_CHARS
                desc += ". Starts:\n    " + head.replace("\n", "\n    ") + (" …" if more else "")
        elif a.text_error:
            desc += f" ({a.text_error})"
        lines.append(desc)
    return "\n".join(lines)


def read_text(a: Attachment, offset: int = 0, max_chars: int = READ_CHARS) -> dict[str, Any]:
    if a.kind == "image":
        return {"error": f"{a.name} is an image; ask about it with look_at_attachment."}
    if a.text is None:
        return {"error": f"{a.name} has no readable text ({a.text_error or 'binary file'}). "
                "Copy it to your workbench with attachment_to_workbench to work on it with code."}
    max_chars = max(500, min(int(max_chars or READ_CHARS), MAX_READ_CHARS))
    offset = max(0, int(offset or 0))
    chunk = a.text[offset:offset + max_chars]
    end = offset + len(chunk)
    out = {"attachment": ref(a.id), "name": a.name, "offset": offset, "total_chars": len(a.text),
           "text": chunk}
    if end < len(a.text):
        out["next_offset"] = end
    return out


def model_image(a: Attachment) -> BinaryContent:
    """An image ready for a model: at most MODEL_IMAGE_SIDE px, PNG or JPEG."""
    with Image.open(io.BytesIO(a.data)) as im:
        im.seek(0)
        frame = im.convert("RGBA") if im.mode in ("RGBA", "LA", "P") else im.convert("RGB")
        frame.thumbnail((MODEL_IMAGE_SIDE, MODEL_IMAGE_SIDE))
        buf = io.BytesIO()
        if frame.mode == "RGBA":
            frame.save(buf, format="PNG")
            media = "image/png"
        else:
            frame.save(buf, format="JPEG", quality=88)
            media = "image/jpeg"
    return BinaryContent(data=buf.getvalue(), media_type=media, identifier=ref(a.id))


def image_png(a: Attachment) -> bytes:
    bc = model_image(a)
    if bc.media_type == "image/png":
        return bc.data
    with Image.open(io.BytesIO(bc.data)) as im:
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()


# --------------------------------------------------------------------------- linking


def link(ids: list[str], message_id: str) -> list[dict[str, Any]]:
    """Attach files to a message; returns their summaries for the message's metadata."""
    out = []
    with db.session() as s:
        for i in ids:
            a = s.get(Attachment, unref(i))
            if a is None:
                continue
            a.message_id = message_id
            s.add(a)
            out.append(a)
        s.commit()
        for a in out:
            s.refresh(a)
    return [summary(a) for a in out]


def made_by_job(job_id: str | None) -> list[Attachment]:
    """Files an agent made during this job that aren't on a message yet."""
    if not job_id:
        return []
    with db.session() as s:
        return list(s.exec(select(Attachment).where(Attachment.job_id == job_id,
                                                    Attachment.message_id == None)  # noqa: E711
                           .order_by(Attachment.created_at)).all())


async def link_outputs(runtime, job_id: str | None, message_id: str | None) -> list[dict[str, Any]]:
    """Put the files a job made on a message (its reply, or its delegation card)."""
    made = made_by_job(job_id)
    if not made or not message_id:
        return []
    with db.session() as s:
        m = s.get(Message, message_id)
        have = list((m.meta or {}).get("attachments") or []) if m else []
    added = link([a.id for a in made], message_id)
    await runtime.update_message(message_id, attachments=have + added)
    return added


# --------------------------------------------------------------------------- history


def strip_images(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Replace images in user prompts and tool returns with a short placeholder.

    The model saw them in this run; later turns get the placeholder and can look again.
    """
    def swap(content):
        if isinstance(content, BinaryContent) and content.is_image:
            name = content.identifier or "image"
            return f"[image {name} was shown here; call look_at_attachment to look again]"
        return content

    for m in messages:
        if not isinstance(m, ModelRequest):
            continue
        for p in m.parts:
            if isinstance(p, UserPromptPart) and not isinstance(p.content, str):
                p.content = [swap(c) for c in p.content]
            elif isinstance(p, ToolReturnPart) and isinstance(p.content, (list, BinaryContent)):
                p.content = ([swap(c) for c in p.content] if isinstance(p.content, list)
                             else swap(p.content))
    return messages


# --------------------------------------------------------------------------- workbench


_has_transfer: dict[str, bool] = {}


async def workbench_server(agent) -> Any:
    """The agent's workbench (a tool server offering ahq_put_file), or None."""
    from .runtime import agent_tool_servers, mcp_toolset

    for srv in agent_tool_servers(agent):
        if srv.id not in _has_transfer:
            try:
                ts = mcp_toolset(srv)
                async with ts:
                    names = {t.name for t in await ts.client.list_tools()}
                _has_transfer[srv.id] = "ahq_put_file" in names
            except Exception:
                continue  # unreachable right now; check again next time
        if _has_transfer[srv.id]:
            return srv
    return None


async def _call(srv, tool: str, args: dict) -> dict:
    from .runtime import mcp_toolset

    ts = mcp_toolset(srv)
    async with ts:
        result = await ts.client.call_tool(tool, args)
    if getattr(result, "isError", False) or getattr(result, "is_error", False):
        text = " ".join(getattr(p, "text", "") for p in result.content).strip()
        raise RuntimeError(text or f"{tool} failed")
    text = " ".join(getattr(p, "text", "") for p in result.content).strip()
    return json.loads(text) if text else {}


async def to_workbench(srv, a: Attachment, path: str) -> dict:
    return await _call(srv, "ahq_put_file", {"path": path,
                                             "data_b64": base64.b64encode(a.data).decode()})


async def from_workbench(srv, path: str) -> tuple[str, bytes]:
    r = await _call(srv, "ahq_get_file", {"path": path, "max_bytes": settings.attachment_max_bytes})
    if r.get("error"):
        raise AttachmentError(r["error"])
    return r.get("name") or PurePosixPath(path).name, base64.b64decode(r["data_b64"])


@dataclass
class Outgoing:
    """A file an agent made, for the tool result."""

    att: Attachment

    def result(self) -> dict:
        return {"attached": True, "attachment": ref(self.att.id), "name": self.att.name,
                "size": human_size(self.att.size),
                "note": "It will appear with your reply (or your result). Mention it briefly."}
