"""Your profile: what every agent should always know about you.

Memory is recalled when it matches the task at hand; the profile is different: it goes into
every agent's instructions, every time (agents can opt out in their settings). Keep it short:
who you are, how you like things done, your environment. A word cap keeps it from crowding
small models.

Agents can suggest additions when a reflection finds a standing preference ("always put
failures first", or the same correction three times). Suggestions always wait for you in
Requests → Profile updates; nothing changes your profile without your say-so.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlmodel import func, select

from . import db
from .models import AgentDef, ProfileProposal, SystemSetting

DEFAULTS: dict[str, Any] = {
    "text": "",
    "enabled": True,
    "max_words": 600,        # what goes into instructions is cut here
    "suggestions": True,     # reflections may suggest additions
    "updated_at": None,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get() -> dict[str, Any]:
    with db.session() as s:
        row = s.get(SystemSetting, "profile")
    return {**DEFAULTS, **(row.value if row else {})}


def save(values: dict[str, Any]) -> dict[str, Any]:
    cur = get()
    merged = {**cur, **{k: v for k, v in values.items() if k in DEFAULTS and k != "updated_at"}}
    merged["text"] = str(merged["text"] or "").strip()
    merged["max_words"] = max(50, min(5000, int(merged["max_words"] or 600)))
    if merged["text"] != cur["text"]:
        merged["updated_at"] = _now().isoformat()
    with db.session() as s:
        row = s.get(SystemSetting, "profile") or SystemSetting(key="profile")
        row.value = merged
        s.add(row)
        s.commit()
    return merged


def words(text: str) -> int:
    return len(text.split())


def clipped(text: str, max_words: int) -> str:
    parts = text.split()
    if len(parts) <= max_words:
        return text
    # Cut at the last line that fits, keeping the markdown intact.
    out, n = [], 0
    for line in text.splitlines():
        k = len(line.split())
        if n + k > max_words:
            break
        out.append(line)
        n += k
    return "\n".join(out).rstrip() + "\n…"


def context_for(agent: AgentDef) -> str:
    """The block that goes into an agent's instructions ('' when off or empty)."""
    if not getattr(agent, "use_profile", True):
        return ""
    p = get()
    if not p["enabled"] or not p["text"]:
        return ""
    return ("## About the user (their profile)\n"
            "Standing facts and preferences from the person you work for. Follow them "
            "unless they tell you otherwise.\n\n" + clipped(p["text"], int(p["max_words"])))


def pending_count() -> int:
    with db.session() as s:
        return s.exec(select(func.count()).select_from(ProfileProposal)
                      .where(ProfileProposal.status == "pending")).one()


def _norm(t: str) -> str:
    return " ".join(t.lower().strip(" .-•*").split())


def already_known(text: str) -> bool:
    """In the profile already, or suggested and still waiting / turned down."""
    n = _norm(text)
    if not n:
        return True
    if n in _norm(get()["text"]):
        return True
    with db.session() as s:
        rows = s.exec(select(ProfileProposal.text).where(
            ProfileProposal.status.in_(("pending", "rejected")))).all()
    return any(_norm(t) == n for t in rows)


def add_line(text: str) -> dict[str, Any]:
    """Append a bullet to the profile."""
    cur = get()["text"]
    line = text.strip()
    if not line.startswith(("-", "*")):
        line = "- " + line
    return save({"text": (cur.rstrip() + "\n" + line) if cur else line})


def decide(proposal_id: str, approve: bool, *, note: str = "", text: str | None = None) -> ProfileProposal:
    with db.session() as s:
        p = s.get(ProfileProposal, proposal_id)
        if p is None:
            raise LookupError("no such suggestion")
        if p.status != "pending":
            raise ValueError(f"this suggestion is already {p.status}")
        if text is not None and text.strip():
            p.text = text.strip()
        if approve:
            add_line(p.text)
        p.status = "approved" if approve else "rejected"
        p.note, p.decided_at = (note or "").strip(), _now()
        s.add(p)
        s.commit()
        s.refresh(p)
        return p
