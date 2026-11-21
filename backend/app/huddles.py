"""Huddles: one conversation with you and several agents.

* A conversation starts with one agent (Thread.agent_id). @mention another agent in a
  message, or add one from the chat header, and it joins: the conversation becomes a huddle
  and moves to the sidebar's Huddles section, keeping its history and address.
* An @mention addresses an agent; several mentions get answers in the order you wrote them,
  each agent seeing the replies before its own. A message that mentions nobody goes to the
  lead (Thread.lead_agent_id, else the agent the conversation started with).
* @huddle (or @all) addresses every member at once. They answer at the same time, each as
  soon as it has an answer, so they don't see each other's replies to that message.
* Agents can @mention other members in their replies, which gives that member the next
  turn. The conversations settings cap how many such turns one message of yours can set
  off (huddle_agent_turns), so two agents can't keep handing the floor to each other.
* Each agent keeps its own model-level history (its own checkpoint, so one agent never sees
  another's tool calls as its own). What the others said since an agent last spoke is put
  in front of its turn as plain text (catch_up). A new member gets the conversation so far,
  trimmed to fit its context window.
* Delegation, approvals, memory and tracing are per agent as before: each answer is a turn
  run by that agent, with its own memory scope, in a span named after it.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlmodel import select

from . import db
from .models import AgentDef, Message, Role, Thread


class HuddleError(ValueError):
    """A member change that isn't allowed; the message says why."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- who's in it


def members(thread: Thread) -> list[str]:
    """Agent IDs in the conversation, in the order they joined (the starter first)."""
    have = list(thread.members or [])
    return have if have else [thread.agent_id]


def lead(thread: Thread) -> str:
    m = members(thread)
    if thread.lead_agent_id and thread.lead_agent_id in m:
        return thread.lead_agent_id
    return thread.agent_id if thread.agent_id in m else m[0]


def is_member(thread: Thread, agent_id: str | None) -> bool:
    return bool(agent_id) and agent_id in members(thread)


def checkpoint_key(thread: Thread | str, agent_id: str, starter: str | None = None) -> str:
    """Where an agent's model-level history for this conversation is kept. The agent the
    conversation started with keeps the conversation's own key (so conversations from
    before huddles carry on as they were); every other member gets its own."""
    tid = thread if isinstance(thread, str) else thread.id
    first = starter if isinstance(thread, str) else thread.agent_id
    return tid if agent_id == first else f"{tid}:{agent_id}"


def checkpoint_keys(thread: Thread) -> list[str]:
    """Every checkpoint key the conversation may have (for deleting them)."""
    ids = list(dict.fromkeys([thread.agent_id, *members(thread)]))
    return [checkpoint_key(thread, a) for a in ids]


def _joinable(agent: AgentDef | None) -> bool:
    return agent is not None and agent.enabled and not agent.is_subagent


def candidates() -> list[AgentDef]:
    """Agents that can be in a huddle: enabled, and not sub-agents."""
    with db.session() as s:
        return [a for a in s.exec(select(AgentDef)).all() if _joinable(a)]


# --------------------------------------------------------------------------- mentions


def _norm(name: str) -> str:
    return re.sub(r"[\s_\-.]+", "", name).lower()


def mentions(text: str, agents: list[AgentDef] | None = None) -> list[str]:
    """Agent IDs @mentioned in text, in the order they appear, each once.

    Names match without regard to case, and with or without their spaces, hyphens or
    underscores: "@Snake Eyes", "@snake-eyes" and "@SnakeEyes" are the same agent. The
    longest name wins ("@Duke Jr" is not "@Duke"). An @ inside a word (an email address)
    isn't a mention.
    """
    if "@" not in text:
        return []
    agents = candidates() if agents is None else agents
    # Patterns per agent: its name with any run of separators allowed between characters
    # groups, longest names first.
    found: list[tuple[int, str]] = []
    taken: list[tuple[int, int]] = []
    for a in sorted(agents, key=lambda a: len(a.name), reverse=True):
        words = [w for w in re.split(r"[\s_\-.]+", a.name.strip()) if w]
        if not words:
            continue
        pat = r"[\s_\-.]?".join(re.escape(w) for w in words)
        for m in re.finditer(rf"(?<![\w@])@({pat})(?![\w])", text, re.I):
            span = (m.start(), m.end())
            if any(s < span[1] and span[0] < e for s, e in taken):
                continue  # part of a longer name already matched
            taken.append(span)
            found.append((m.start(), a.id))
    out: list[str] = []
    for _, aid in sorted(found):
        if aid not in out:
            out.append(aid)
    return out


def addressed(text: str, agents: list[AgentDef]) -> list[str]:
    """Agents an agent's reply speaks to by name without the @, in its last paragraph:
    "Duke, want to weigh in?", "Duke: can you check this?" or "What do you think, Duke?".
    Models often hand off that way even when told to write @Name. Only the last paragraph
    counts, where a hand-off goes, so a name used in passing ("Duke said…", a "Duke: …"
    line in a list earlier on) isn't read as one. Same order and matching as mentions()."""
    paras = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    if not paras:
        return []
    last = re.sub(r"[*_`]+", "", paras[-1])  # **Duke**, want to… → Duke, want to…
    found: list[tuple[int, str]] = []
    for a in sorted(agents, key=lambda a: len(a.name), reverse=True):
        words = [w for w in re.split(r"[\s_\-.]+", a.name.strip()) if w]
        if not words:
            continue
        name = r"[\s_\-.]?".join(re.escape(w) for w in words)
        lead_in = r"(?:^|(?<=[.!?])\s+|\n\s*)(?:(?:hey|so|and|ok|okay|over to you),?\s+)?"
        pats = [rf"{lead_in}({name})\s*[,:\u2014\u2013]",          # Duke, … / Duke: …
                rf",\s*({name})\s*[?!.]?\s*$",                       # …, Duke?
                rf",\s*({name})\s*[?!]"]                             # …, Duke? More text
        for pat in pats:
            m = re.search(pat, last, re.I | re.M)
            if m and not re.match(r"[\w@]", last[m.start(1) - 1: m.start(1)] or " "):
                found.append((m.start(1), a.id))
                break
    out: list[str] = []
    for _, aid in sorted(found):
        if aid not in out:
            out.append(aid)
    return out


EVERYONE = ("huddle", "all")  # @huddle / @all: every member, unless an agent has that name


def everyone(text: str, agents: list[AgentDef] | None = None) -> bool:
    """Whether text addresses the whole huddle with @huddle or @all."""
    if "@" not in text:
        return False
    agents = candidates() if agents is None else agents
    taken = {_norm(a.name) for a in agents}
    words = [w for w in EVERYONE if w not in taken]
    return bool(words) and re.search(
        rf"(?<![\w@])@(?:{'|'.join(words)})(?![\w])", text, re.I) is not None


# --------------------------------------------------------------------------- changes


def add_member(thread_id: str, agent_id: str) -> tuple[Thread, bool]:
    """Add an agent to the conversation. Returns the thread and whether it joined now
    (False: it was already in it). Raises HuddleError."""
    with db.session() as s:
        t = s.get(Thread, thread_id)
        if t is None or t.deleted_at is not None:
            raise HuddleError("conversation not found")
        agent = s.get(AgentDef, agent_id)
        if not _joinable(agent):
            raise HuddleError("that agent can't join (it's disabled, a sub-agent, or gone)")
        have = members(t)
        if agent_id in have:
            return t, False
        t.members = [*have, agent_id]  # reassign so the JSON column is saved
        t.huddle = True
        s.add(t)
        s.commit()
        s.refresh(t)
        return t, True


def remove_member(thread_id: str, agent_id: str) -> Thread:
    """Take an agent out of the conversation. The last member can't leave; if the lead
    leaves, the first remaining member leads. The conversation stays a huddle."""
    with db.session() as s:
        t = s.get(Thread, thread_id)
        if t is None or t.deleted_at is not None:
            raise HuddleError("conversation not found")
        have = members(t)
        if agent_id not in have:
            raise HuddleError("that agent isn't in this conversation")
        if len(have) == 1:
            raise HuddleError("a conversation needs at least one agent")
        was_lead = lead(t) == agent_id
        t.members = [a for a in have if a != agent_id]
        if was_lead:
            t.lead_agent_id = t.members[0]
        s.add(t)
        s.commit()
        s.refresh(t)
        return t


def set_lead(thread_id: str, agent_id: str) -> Thread:
    with db.session() as s:
        t = s.get(Thread, thread_id)
        if t is None or t.deleted_at is not None:
            raise HuddleError("conversation not found")
        if agent_id not in members(t):
            raise HuddleError("only a member can lead; add the agent first")
        t.lead_agent_id = agent_id
        s.add(t)
        s.commit()
        s.refresh(t)
        return t


def forget_agent(s, agent_ids: list[str]) -> list[str]:
    """Take deleted agents out of the huddles they joined (not ones they started: those
    go with them). Returns the checkpoint keys they had there. Uses the caller's session."""
    keys: list[str] = []
    for t in s.exec(select(Thread).where(Thread.huddle == True)).all():  # noqa: E712
        if t.agent_id in agent_ids:
            continue
        have = members(t)
        gone = [a for a in have if a in agent_ids]
        if not gone:
            continue
        keys += [checkpoint_key(t, a) for a in gone]
        t.members = [a for a in have if a not in agent_ids] or [t.agent_id]
        if t.lead_agent_id in agent_ids:
            t.lead_agent_id = None
        s.add(t)
    return keys


# --------------------------------------------------------------------------- the turn


def _names() -> dict[str, str]:
    with db.session() as s:
        return {a.id: a.name for a in s.exec(select(AgentDef)).all()}


def roster_note(agent: AgentDef, thread: Thread) -> str:
    """For the agent's instructions: who else is in the huddle and how mentions work."""
    names = _names()
    others = [a for a in members(thread) if a != agent.id]
    if not thread.huddle or not others:
        return ""
    lead_id = lead(thread)
    listed = ", ".join(names.get(a, "a removed agent") + (" (lead)" if a == lead_id else "")
                       for a in others)
    you_lead = " You lead it: you answer messages that mention nobody." if lead_id == agent.id else ""
    example = names.get(others[0], "Name")
    return ("This conversation is a huddle: the user and several agents in one chat. "
            f"The other agents here: {listed}.{you_lead} Your replies are shown with your name, "
            "and what the others said since you last spoke is given to you at the start of "
            "your turn. Answer what is asked of you; don't repeat what another agent already "
            "said. To hand a question to another agent here, @mention them in your reply, "
            f"with the @ sign: write @Name (for example \"@{example}, can you check this?\"), not "
            "just the name. They answer next; without the @ they may never see it. Only "
            "mention someone when you want them to respond. Delegating works as usual, except "
            "that an agent you delegate to joins the huddle and posts its findings itself "
            "under its own name.")


def catch_up(thread: Thread, agent_id: str, *, exclude_message_id: str | None = None,
             budget_chars: int = 12000) -> str:
    """What was said in the huddle since this agent last spoke, as plain text for the start
    of its turn ("" if nothing). A member that hasn't spoken yet gets the whole conversation,
    trimmed from the oldest end to `budget_chars`."""
    names = _names()
    with db.session() as s:
        # Its last reply in a turn of its own (findings it posted from a delegated task
        # aren't in its model history, so they still count as news for it).
        mine = next((m.created_at for m in s.exec(select(Message).where(
            Message.thread_id == thread.id, Message.role == Role.agent,
            Message.agent_id == agent_id).order_by(Message.created_at.desc())).all()
            if (m.meta or {}).get("kind") != "huddle_finding"), None)
        q = select(Message).where(Message.thread_id == thread.id,
                                  Message.role.in_([Role.user, Role.agent]))
        if mine is not None:
            q = q.where(Message.created_at > mine)
        rows = s.exec(q.order_by(Message.created_at)).all()
    lines: list[str] = []
    for m in rows:
        if m.id == exclude_message_id:
            continue
        finding = (m.meta or {}).get("kind") == "huddle_finding"
        if m.role == Role.agent and m.agent_id == agent_id and not finding:
            continue
        if m.role == Role.agent and m.agent_id == agent_id:
            who = ("You (findings you posted from a task "
                   f"{(m.meta or {}).get('requester') or 'another agent'} delegated to you)")
        else:
            who = "The user" if m.role == Role.user else names.get(m.agent_id or "", "An agent")
        text = (m.content or "").strip()
        if (m.meta or {}).get("attachments"):
            text += f" [attached {len(m.meta['attachments'])} file(s)]"
        if text:
            lines.append(f"{who}: {text}")
    if not lines:
        return ""
    head = ("[Huddle: the conversation so far]" if mine is None
            else "[Huddle: what was said since you last spoke]")
    body, size = [], 0
    for line in reversed(lines):
        if size + len(line) > budget_chars and body:
            body.append("(earlier messages left out)")
            break
        if len(line) > budget_chars:
            line = line[: budget_chars - 20] + " …(cut)"
        body.append(line)
        size += len(line) + 1
    return head + "\n" + "\n\n".join(reversed(body)) + "\n[End of huddle catch-up]"


def posted_note(name: str) -> str:
    """For a delegator in a huddle, whose delegate posted its own findings."""
    return (f"{name} has already posted these findings in the huddle under their own name, "
            "and the user has read them. This is their work, not yours: don't repeat or "
            f"restate it. Briefly credit {name} if you refer to it, then add only your own "
            "view, conclusions or next steps.")


def addressed_note(agent: AgentDef, by: str | None) -> str:
    """Why it's this agent's turn, when another agent mentioned it."""
    if not by:
        return ""
    return f"[{by} mentioned you in the huddle and is waiting for your answer.]"
