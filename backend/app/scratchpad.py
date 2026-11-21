"""Scratch pads: notes an agent keeps while it works, outside its context window.

Every job (a turn or a delegated task) gets its own pad. An agent writes down each finding
as it finds it, with its source (note), and reads its notes back in pages before it
answers (read_notes). Older tool results get shortened as a run grows; what matters from
them is already in the notes, so a long research task doesn't lose its first findings, and
a reply that gets cut off doesn't take the work with it.

The delegator, or any agent in the same conversation, can read a pad while it exists
(read_notes with the job ID), and you can watch one fill on the delegation card.

Pads live in Valkey only, never in Postgres:
  ahq:pad:<job_id>       list of notes (JSON: n, at, text)
  ahq:padinfo:<job_id>   hash: thread, agent, bytes, count
They're kept while the job runs and AHQ_PAD_TTL_HOURS (24) after it finishes, then expire.
A pad holds at most AHQ_PAD_MAX_BYTES (64 KB). "Keep as file" on the card saves one as a
conversation file (a Markdown attachment), the only way a pad reaches the database.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .config import settings

# While the job runs: long enough for any run, refreshed on every note.
RUNNING_TTL = 7 * 24 * 3600
PAGE_CHARS = 6000  # what read_notes returns at most per call

RULES = """\
You have a scratch pad for this job, kept outside your context window. Use it for any work
that takes several steps (research, checks, comparisons): call `note` with each finding as
soon as you have it, in a line or two with its source (title, date, link or command), e.g.
"NVIDIA Newsroom, NVIDIA Announces CEO Transition, Nov 12 2026, https://...: Huang resigns,
CFO named interim." Older tool results are shortened as your run grows, so anything you
will need later must be in your notes. When you're done gathering, call `read_notes`, and
write your answer from your notes. A short task needs no notes."""


class PadFull(Exception):
    pass


def _key(job_id: str) -> str:
    return f"ahq:pad:{job_id}"


def _info(job_id: str) -> str:
    return f"ahq:padinfo:{job_id}"


async def add(r, job_id: str, text: str, *, thread_id: str, agent_id: str) -> dict:
    """Append a note. Returns {n, count, bytes, room}. Raises PadFull."""
    text = text.strip()
    size = len(text.encode("utf-8"))
    used = int(await r.hget(_info(job_id), "bytes") or 0)
    if used + size > settings.pad_max_bytes:
        raise PadFull(f"your scratch pad is full ({settings.pad_max_bytes // 1024} KB). Read your "
                      "notes with read_notes and write your answer, or keep notes shorter.")
    n = int(await r.hincrby(_info(job_id), "count", 1))
    await r.hincrby(_info(job_id), "bytes", size)
    await r.hset(_info(job_id), mapping={"thread": thread_id, "agent": agent_id})
    entry = {"n": n, "at": datetime.now(timezone.utc).isoformat(), "text": text}
    await r.rpush(_key(job_id), json.dumps(entry))
    for k in (_key(job_id), _info(job_id)):
        await r.expire(k, RUNNING_TTL)
    return {"n": n, "count": n, "bytes": used + size, "room": settings.pad_max_bytes - used - size}


async def info(r, job_id: str) -> dict | None:
    """{thread, agent, count, bytes} or None if the job has no pad (or it expired)."""
    raw = await r.hgetall(_info(job_id))
    if not raw:
        return None
    return {"thread": raw.get("thread"), "agent": raw.get("agent"),
            "count": int(raw.get("count") or 0), "bytes": int(raw.get("bytes") or 0)}


async def notes(r, job_id: str) -> list[dict]:
    return [json.loads(x) for x in await r.lrange(_key(job_id), 0, -1)]


async def page(r, job_id: str, offset: int = 0) -> dict:
    """Notes from number offset+1 on, as many as fit in PAGE_CHARS."""
    all_ = await notes(r, job_id)
    out, size = [], 0
    for e in all_[max(0, offset):]:
        line = f"{e['n']}. {e['text']}"
        if out and size + len(line) > PAGE_CHARS:
            break
        out.append(line)
        size += len(line) + 1
    nxt = max(0, offset) + len(out)
    return {"count": len(all_), "notes": "\n".join(out) or "(no notes)",
            **({"next_offset": nxt} if nxt < len(all_) else {})}


async def finished(r, job_id: str) -> None:
    """The job is done: keep its pad AHQ_PAD_TTL_HOURS more, then let it expire."""
    ttl = max(60, int(settings.pad_ttl_hours * 3600))
    for k in (_key(job_id), _info(job_id)):
        await r.expire(k, ttl)


async def expires_in(r, job_id: str) -> int | None:
    t = await r.ttl(_key(job_id))
    return t if t and t > 0 else None


def markdown(agent_name: str, task: str, entries: list[dict]) -> str:
    lines = [f"# {agent_name}'s notes", ""]
    if task:
        lines += [f"Task: {task.strip()}", ""]
    for e in entries:
        lines.append(f"{e['n']}. {e['text']}")
    return "\n".join(lines) + "\n"
