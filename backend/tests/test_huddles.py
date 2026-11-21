"""Huddles: several agents in one conversation, addressed with @mentions (app/huddles.py)."""

from __future__ import annotations

import asyncio

from pydantic_ai import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo

from app import db, huddles, memory
from app.models import AgentDef, Role, Thread

from .conftest import SCRIPTS, _instructions, _last_prompt

SEEN: dict[str, list[dict]] = {}


def recorder(tag: str, reply: str | None = None):
    """Notes what the agent was given (instructions, prompt, how many earlier requests in
    its own history), then answers `reply` or "<tag> here"."""
    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        SEEN.setdefault(tag, []).append({
            "instructions": _instructions(messages), "prompt": _last_prompt(messages),
            "requests": sum(1 for m in messages if getattr(m, "kind", "") == "request"),
            "history": " ".join(str(getattr(p, "content", "")) for m in messages
                                for p in getattr(m, "parts", [])),
        })
        return ModelResponse(parts=[TextPart(reply or f"{tag} here")])
    return fn


SCRIPTS.update({"h-duke": recorder("duke"), "h-scarlett": recorder("scarlett"),
                "h-snake": recorder("snake"),
                "h-alpha": recorder("alpha", "Over to @Bravo for a check"),
                "h-bravo": recorder("bravo", "Back to @Alpha, looks right"),
                "h-asker": recorder("asker", "Here's my answer.\n\nDuke, want to weigh in on the hardware side?")})


async def _crew(hq):
    SEEN.clear()
    duke = await hq.agent(name="Duke", model="fake:h-duke")
    scarlett = await hq.agent(name="Scarlett", model="fake:h-scarlett")
    snake = await hq.agent(name="Snake Eyes", model="fake:h-snake")
    t = await hq.thread(duke["id"])
    return duke, scarlett, snake, t


def _thread(tid: str) -> Thread:
    with db.session() as s:
        return s.get(Thread, tid)


def _agents_replying(hq, tid):
    return [m.agent_id for m in hq.messages(tid) if m.role == Role.agent]


# --------------------------------------------------------------------------- mentions


def test_mentions_match_names_loosely_in_order():
    a = AgentDef(id="a", name="Duke")
    b = AgentDef(id="b", name="Snake Eyes")
    c = AgentDef(id="c", name="Hi-Tech")
    d = AgentDef(id="d", name="Duke Jr")
    agents = [a, b, c, d]
    assert huddles.mentions("@snake eyes and @Duke, then @SnakeEyes again", agents) == ["b", "a"]
    assert huddles.mentions("ask @hitech or @Hi Tech", agents) == ["c"]
    assert huddles.mentions("@Duke Jr please", agents) == ["d"]       # the longer name wins
    assert huddles.mentions("mail duke@example.com", agents) == []    # not a mention
    assert huddles.mentions("@Dukes are not @Duke's", agents) == ["a"]
    assert huddles.mentions("no mentions here", agents) == []


def test_names_addressed_without_the_at_at_the_end_of_a_reply():
    duke = AgentDef(id="d", name="Duke")
    snake = AgentDef(id="s", name="Snake Eyes")
    agents = [duke, snake]
    long_reply = ("Quick read on this week.\n\nMy read: confidence is bifurcating.\n\n"
                  "Duke, want to weigh in from a network/hardware angle on the datacenter capex side?")
    assert huddles.addressed(long_reply, agents) == ["d"]
    assert huddles.addressed("Done. **Snake Eyes:** can you check the page?", agents) == ["s"]
    assert huddles.addressed("That's my take. What do you think, Duke?", agents) == ["d"]
    assert huddles.addressed("Hey Duke, over to you.", agents) == ["d"]
    # A name in passing, or anywhere but the last paragraph, isn't a hand-off.
    assert huddles.addressed("Duke said the backups ran fine.", agents) == []
    assert huddles.addressed("Duke: owns backups\nSnake Eyes: browsing\n\nAll set.", agents) == []
    assert huddles.addressed("I agree with Duke's plan.", agents) == []
    assert huddles.addressed("Ask the Dukes of Hazzard, really.", agents) == []


def test_checkpoint_keys():
    t = Thread(id="t1", agent_id="duke", members=["duke", "scarlett"], huddle=True)
    assert huddles.checkpoint_key(t, "duke") == "t1"           # the starter keeps the old key
    assert huddles.checkpoint_key(t, "scarlett") == "t1:scarlett"
    assert huddles.checkpoint_keys(t) == ["t1", "t1:scarlett"]
    assert huddles.lead(t) == "duke"
    assert huddles.lead(Thread(id="t2", agent_id="duke", members=["scarlett"])) == "scarlett"


# --------------------------------------------------------------------------- turns


async def test_no_mention_goes_to_the_lead_and_stays_a_chat(hq):
    await hq.start_worker()
    duke, _, _, t = await _crew(hq)
    await hq.say(t["id"], "hello there")
    assert await hq.wait_agent_replies(t["id"], 1) == ["duke here"]
    th = _thread(t["id"])
    assert not th.huddle and huddles.members(th) == [duke["id"]]
    assert "huddle" not in SEEN["duke"][0]["instructions"].lower()
    assert not SEEN["duke"][0]["prompt"].startswith("[Huddle")


async def test_mentioning_an_agent_makes_a_huddle(hq):
    await hq.start_worker()
    duke, scarlett, _, t = await _crew(hq)
    await hq.say(t["id"], "hello Duke")
    await hq.wait_agent_replies(t["id"], 1)

    r = await hq.http.post(f"/api/threads/{t['id']}/messages",
                           json={"content": "@Scarlett what do you think?"})
    assert r.status_code == 202 and r.json()["agent_id"] == scarlett["id"]
    assert r.json()["thread"]["huddle"] is True
    assert await hq.wait_agent_replies(t["id"], 2) == ["duke here", "scarlett here"]
    th = _thread(t["id"])
    assert th.huddle and huddles.members(th) == [duke["id"], scarlett["id"]]
    joined = [m for m in hq.messages(t["id"]) if (m.meta or {}).get("kind") == "huddle_join"]
    assert [m.content for m in joined] == ["Scarlett joined the huddle."]
    assert _agents_replying(hq, t["id"]) == [duke["id"], scarlett["id"]]

    # The new member gets the conversation so far, without your message twice, and none
    # of Duke's model-level history (its own checkpoint).
    first = SEEN["scarlett"][0]
    assert first["prompt"].startswith("[Huddle: the conversation so far]")
    assert "The user: hello Duke" in first["prompt"] and "Duke: duke here" in first["prompt"]
    assert first["prompt"].count("@Scarlett what do you think?") == 1
    assert first["requests"] == 1
    assert "Duke" in first["instructions"] and "huddle" in first["instructions"]

    # No mention: back to the lead, who hears what Scarlett said since it last spoke.
    await hq.say(t["id"], "thanks, back to you")
    assert (await hq.wait_agent_replies(t["id"], 3))[-1] == "duke here"
    again = SEEN["duke"][-1]
    assert again["prompt"].startswith("[Huddle: what was said since you last spoke]")
    assert "Scarlett: scarlett here" in again["prompt"]
    assert "hello Duke" not in again["prompt"]  # already in its own history
    assert "hello Duke" in again["history"] and again["requests"] >= 2
    assert "Scarlett" in again["instructions"] and "You lead it" in again["instructions"]

    # Huddles are listed for every member.
    r = await hq.http.get("/api/threads", params={"agent_id": scarlett["id"]})
    assert [x["id"] for x in r.json()] == [t["id"]]


async def test_several_mentions_answer_in_order_and_see_each_other(hq):
    await hq.start_worker(concurrency=4)
    duke, scarlett, snake, t = await _crew(hq)
    await hq.say(t["id"], "@Snake Eyes and @Scarlett: is the lab up?")
    await hq.wait_agent_replies(t["id"], 2)
    assert _agents_replying(hq, t["id"]) == [snake["id"], scarlett["id"]]
    assert huddles.members(_thread(t["id"])) == [duke["id"], snake["id"], scarlett["id"]]
    # Scarlett answered second and saw Snake Eyes' reply first.
    assert "Snake Eyes: snake here" in SEEN["scarlett"][0]["prompt"]
    assert "is the lab up?" in SEEN["scarlett"][0]["prompt"]
    assert "duke" not in SEEN  # Duke wasn't asked


async def test_agents_mentioning_each_other_are_capped(hq):
    await hq.start_worker()
    SEEN.clear()
    alpha = await hq.agent(name="Alpha", model="fake:h-alpha")
    bravo = await hq.agent(name="Bravo", model="fake:h-bravo")
    t = await hq.thread(alpha["id"])
    await hq.http.post(f"/api/threads/{t['id']}/members", json={"agent_id": bravo["id"]})
    await hq.say(t["id"], "draft the plan")
    # Alpha, then three agent-started turns (the default cap): Bravo, Alpha, Bravo.
    await hq.wait_agent_replies(t["id"], 4)
    await asyncio.sleep(0.3)
    assert _agents_replying(hq, t["id"]) == [alpha["id"], bravo["id"], alpha["id"], bravo["id"]]
    limit = [m for m in hq.messages(t["id"]) if (m.meta or {}).get("kind") == "huddle_limit"]
    assert len(limit) == 1 and "Alpha" in limit[0].content
    assert SEEN["bravo"][0]["prompt"].endswith("answer it.")
    assert "Alpha: Over to @Bravo" in SEEN["bravo"][0]["prompt"]

    # With the setting at 0, agents' mentions start nothing.
    r = await hq.http.put("/api/conversations/settings", json={"huddle_agent_turns": 0})
    assert r.json()["huddle_agent_turns"] == 0
    await hq.say(t["id"], "once more")
    await hq.wait_agent_replies(t["id"], 5)
    await asyncio.sleep(0.3)
    assert len([m for m in hq.messages(t["id"]) if m.role == Role.agent]) == 5


async def test_reports_and_events_go_to_the_member_they_belong_to(hq):
    await hq.start_worker()
    duke, scarlett, _, t = await _crew(hq)
    await hq.http.post(f"/api/threads/{t['id']}/members", json={"agent_id": scarlett["id"]})
    rt = hq.runtime
    job = await rt.enqueue_turn(t["id"], "[event] for Scarlett", from_user=False, agent_id=scarlett["id"])
    assert job.assignee_agent_id == scarlett["id"]
    stranger = await hq.agent(name="Stranger", model="fake:echo")
    job = await rt.enqueue_turn(t["id"], "[event] not a member", from_user=False, agent_id=stranger["id"])
    assert job.assignee_agent_id == duke["id"]  # not in it: the lead takes it
    await hq.wait_agent_replies(t["id"], 2)


# --------------------------------------------------------------------------- members


async def test_members_lead_and_leaving(hq):
    await hq.start_worker()
    duke, scarlett, snake, t = await _crew(hq)
    tid = t["id"]
    r = await hq.http.post(f"/api/threads/{tid}/members", json={"agent_id": scarlett["id"]})
    assert r.status_code == 200 and r.json()["huddle"] and r.json()["members"] == [duke["id"], scarlett["id"]]
    # Adding twice changes nothing (and says nothing more).
    await hq.http.post(f"/api/threads/{tid}/members", json={"agent_id": scarlett["id"]})
    assert len([m for m in hq.messages(tid) if (m.meta or {}).get("kind") == "huddle_join"]) == 1

    assert (await hq.http.put(f"/api/threads/{tid}/lead", json={"agent_id": snake["id"]})).status_code == 422
    r = await hq.http.put(f"/api/threads/{tid}/lead", json={"agent_id": scarlett["id"]})
    assert r.status_code == 200 and r.json()["lead_agent_id"] == scarlett["id"]
    await hq.say(tid, "who answers now?")
    assert (await hq.wait_agent_replies(tid, 1)) == ["scarlett here"]

    # The lead leaves: the first remaining member leads. It stays a huddle.
    r = await hq.http.delete(f"/api/threads/{tid}/members/{scarlett['id']}")
    assert r.status_code == 200 and r.json()["members"] == [duke["id"]] and r.json()["huddle"]
    assert huddles.lead(_thread(tid)) == duke["id"]
    assert (await hq.http.delete(f"/api/threads/{tid}/members/{duke['id']}")).status_code == 422
    kinds = [(m.meta or {}).get("kind") for m in hq.messages(tid) if m.role == Role.event]
    assert kinds == ["huddle_join", "huddle_lead", "huddle_leave"]

    # Disabled agents and sub-agents can't join.
    snake["enabled"] = False
    await hq.http.put(f"/api/agents/{snake['id']}", json=snake)
    assert (await hq.http.post(f"/api/threads/{tid}/members",
                               json={"agent_id": snake["id"]})).status_code == 422


async def test_memory_scopes_follow_the_agent_answering(hq):
    await hq.start_worker()
    duke, scarlett, _, t = await _crew(hq)
    await memory.add("Scarlett's own note: the RSS feed list lives in feeds.opml",
                     scope="agent", agent_id=scarlett["id"])
    await memory.add("Duke's own note: the RSS feed for the lab is on gitgard",
                     scope="agent", agent_id=duke["id"])
    await hq.say(t["id"], "@Scarlett where is the RSS feed list?")
    await hq.wait_agent_replies(t["id"], 1)
    await hq.say(t["id"], "and you, Duke? where is the RSS feed list?")
    await hq.wait_agent_replies(t["id"], 2)
    s_ins, d_ins = SEEN["scarlett"][0]["instructions"], SEEN["duke"][0]["instructions"]
    assert "feeds.opml" in s_ins and "gitgard" not in s_ins
    assert "gitgard" in d_ins and "feeds.opml" not in d_ins


async def test_deleting_a_member_keeps_the_huddle(hq):
    await hq.start_worker()
    duke, scarlett, _, t = await _crew(hq)
    await hq.say(t["id"], "@Scarlett hi")
    await hq.wait_agent_replies(t["id"], 1)
    scarlett["enabled"] = False
    await hq.http.put(f"/api/agents/{scarlett['id']}", json=scarlett)
    r = await hq.http.delete(f"/api/agents/{scarlett['id']}/permanent")
    assert r.status_code in (200, 204), r.text
    th = _thread(t["id"])
    assert th is not None and huddles.members(th) == [duke["id"]]
    assert any(m.role == Role.user for m in hq.messages(t["id"]))

    # Deleting the agent who started it deletes the huddle with it.
    snake = await hq.agent(name="Snake Eyes 2", model="fake:h-snake")
    t2 = await hq.thread(snake["id"])
    await hq.http.post(f"/api/threads/{t2['id']}/members", json={"agent_id": duke["id"]})
    snake["enabled"] = False
    await hq.http.put(f"/api/agents/{snake['id']}", json=snake)
    assert (await hq.http.delete(f"/api/agents/{snake['id']}/permanent")).status_code in (200, 204)
    assert _thread(t2["id"]) is None


async def test_an_agent_addressing_a_member_by_name_hands_off(hq):
    await hq.start_worker()
    SEEN.clear()
    asker = await hq.agent(name="Hi-Tech", model="fake:h-asker")
    duke = await hq.agent(name="Duke", model="fake:h-duke")
    t = await hq.thread(asker["id"])
    await hq.http.post(f"/api/threads/{t['id']}/members", json={"agent_id": duke["id"]})
    await hq.say(t["id"], "What do you all think about the market?")
    await hq.wait_agent_replies(t["id"], 2)
    assert _agents_replying(hq, t["id"]) == [asker["id"], duke["id"]]
    assert "Hi-Tech mentioned you" in SEEN["duke"][0]["prompt"]
    assert "@Duke, can you check this?" in SEEN["asker"][0]["instructions"]


# --------------------------------------------------------------------------- delegation


def _recording_boss(background: bool):
    from .conftest import _tool_returns, boss
    inner = boss(background)

    def fn(messages, info):
        prompt = _last_prompt(messages)
        SEEN.setdefault("boss", []).append({
            "prompt": prompt, "returns": [r.content for r in _tool_returns(messages)]})
        if "[Background job" in prompt:  # after the huddle catch-up, so not at the start
            return ModelResponse(parts=[TextPart("Update: see Scarlett's findings above.")])
        return inner(messages, info)
    return fn


SCRIPTS.update({"h-boss": _recording_boss(False), "h-boss-bg": _recording_boss(True)})


async def test_delegating_from_a_huddle_brings_the_delegate_in(hq):
    await hq.start_worker(concurrency=4)
    SEEN.clear()
    scarlett = await hq.agent(name="Scarlett", model="fake:worker")
    duke = await hq.agent(name="Duke", model="fake:h-boss", delegates=[scarlett["id"]])
    hitech = await hq.agent(name="Hi-Tech", model="fake:h-snake")
    t = await hq.thread(hitech["id"])
    await hq.http.post(f"/api/threads/{t['id']}/members", json={"agent_id": duke["id"]})
    await hq.say(t["id"], "@Duke check the market claims")
    await hq.wait_agent_replies(t["id"], 2)
    msgs = hq.messages(t["id"])
    agent_msgs = [m for m in msgs if m.role == Role.agent]
    # Scarlett joined, posted her findings under her own name, then Duke answered.
    assert [m.agent_id for m in agent_msgs] == [scarlett["id"], duke["id"]]
    finding = agent_msgs[0]
    assert finding.meta["kind"] == "huddle_finding" and finding.meta["requester"] == "Duke"
    assert finding.content.startswith("Worker finished") and "Please handle" in finding.content
    assert huddles.members(_thread(t["id"])) == [hitech["id"], duke["id"], scarlett["id"]]
    assert "Scarlett joined the huddle." in [m.content for m in msgs if m.role == Role.event]
    # Duke is told she already posted it: credit her, don't repeat it.
    note = SEEN["boss"][-1]["returns"][-1]["note"]
    assert "already posted these findings" in note and "Scarlett" in note
    # Scarlett's own findings aren't in her model history, so her next turn hears them.
    heard = huddles.catch_up(_thread(t["id"]), scarlett["id"])
    assert "You (findings you posted from a task Duke delegated to you): Worker finished" in heard


async def test_background_delegation_in_a_huddle_reports_without_repeating(hq):
    await hq.start_worker(concurrency=4)
    SEEN.clear()
    scarlett = await hq.agent(name="Scarlett", model="fake:worker")
    duke = await hq.agent(name="Duke", model="fake:h-boss-bg", delegates=[scarlett["id"]])
    hitech = await hq.agent(name="Hi-Tech", model="fake:h-snake")
    t = await hq.thread(hitech["id"])
    await hq.http.post(f"/api/threads/{t['id']}/members", json={"agent_id": duke["id"]})
    await hq.say(t["id"], "@Duke look into it")
    # Duke's first reply, Scarlett's findings, Duke's report turn.
    await hq.wait_agent_replies(t["id"], 3)
    who = [m.agent_id for m in hq.messages(t["id"]) if m.role == Role.agent]
    assert who.count(scarlett["id"]) == 1 and who[-1] == duke["id"]
    report = [x["prompt"] for x in SEEN["boss"] if "[Background job" in x["prompt"]]
    assert report and "already posted these findings" in report[0]


async def test_delegating_outside_a_huddle_stays_in_the_background(hq):
    await hq.start_worker(concurrency=4)
    scarlett = await hq.agent(name="Scarlett", model="fake:worker")
    duke = await hq.agent(name="Duke", model="fake:boss", delegates=[scarlett["id"]])
    t = await hq.thread(duke["id"])
    await hq.say(t["id"], "check it")
    await hq.wait_agent_replies(t["id"], 1)
    th = _thread(t["id"])
    assert not th.huddle and huddles.members(th) == [duke["id"]]
    assert [m.agent_id for m in hq.messages(t["id"]) if m.role == Role.agent] == [duke["id"]]


# --------------------------------------------------------------------------- @huddle


def slow(tag: str, seconds: float = 0.6):
    import time

    async def fn(messages, info):
        start = time.monotonic()
        await asyncio.sleep(seconds)
        SEEN.setdefault(tag, []).append({"prompt": _last_prompt(messages), "start": start,
                                         "end": time.monotonic()})
        return ModelResponse(parts=[TextPart(f"{tag} here")])
    return fn


SCRIPTS.update({"h-slow-a": slow("a"), "h-slow-b": slow("b"), "h-slow-c": slow("c")})


def test_everyone_words():
    agents = [AgentDef(id="d", name="Duke")]
    assert huddles.everyone("@huddle what do you all think?", agents)
    assert huddles.everyone("thoughts, @All?", agents)
    assert not huddles.everyone("the huddle@example.com list", agents)
    assert not huddles.everyone("@huddlers", agents)
    # An agent called All keeps its name: @all is that agent, @huddle is still everyone.
    agents.append(AgentDef(id="x", name="All"))
    assert not huddles.everyone("@all hi", agents) and huddles.everyone("@huddle hi", agents)


async def test_at_huddle_asks_every_member_at_once(hq):
    await hq.start_worker(concurrency=4)
    SEEN.clear()
    a = await hq.agent(name="Ann", model="fake:h-slow-a")
    b = await hq.agent(name="Ben", model="fake:h-slow-b")
    c = await hq.agent(name="Cal", model="fake:h-slow-c")
    t = await hq.thread(a["id"])
    for x in (b, c):
        await hq.http.post(f"/api/threads/{t['id']}/members", json={"agent_id": x["id"]})
    await hq.say(t["id"], "first, hello")  # Ann (lead) answers alone
    await hq.wait_agent_replies(t["id"], 1)
    await hq.say(t["id"], "@huddle what's your view?")
    await hq.wait_agent_replies(t["id"], 4)
    who = [m.agent_id for m in hq.messages(t["id"]) if m.role == Role.agent][1:]
    assert sorted(who) == sorted([a["id"], b["id"], c["id"]])
    # They ran at the same time: every turn started before any of them finished.
    runs = [SEEN["a"][-1], SEEN["b"][-1], SEEN["c"][-1]]
    assert max(r["start"] for r in runs) < min(r["end"] for r in runs)
    for r in runs:
        assert "addressed everyone in the huddle" in r["prompt"]
        assert "what's your view?" in r["prompt"]
        # They hear the earlier reply, not each other's answers to this message.
        assert "b here" not in r["prompt"] and "c here" not in r["prompt"]
    assert "Ann: a here" in SEEN["b"][-1]["prompt"]
    turns = [j for j in hq.jobs() if j.kind == "turn"]
    assert len({(j.meta or {}).get("root") for j in turns[-3:]}) == 1


async def test_at_huddle_in_a_one_agent_chat_is_just_the_agent(hq):
    await hq.start_worker()
    SEEN.clear()
    duke = await hq.agent(name="Duke", model="fake:h-duke")
    t = await hq.thread(duke["id"])
    await hq.say(t["id"], "@huddle anyone?")
    assert await hq.wait_agent_replies(t["id"], 1) == ["duke here"]
    assert not _thread(t["id"]).huddle


async def test_hand_offs_after_at_huddle_share_one_cap(hq):
    await hq.start_worker(concurrency=4)
    SEEN.clear()
    alpha = await hq.agent(name="Alpha", model="fake:h-alpha")
    bravo = await hq.agent(name="Bravo", model="fake:h-bravo")
    t = await hq.thread(alpha["id"])
    await hq.http.post(f"/api/threads/{t['id']}/members", json={"agent_id": bravo["id"]})
    await hq.say(t["id"], "@huddle draft the plan")
    # Both answer (2), then at most 3 hand-off turns between them in all (not 3 each).
    await hq.wait_agent_replies(t["id"], 5)
    await asyncio.sleep(0.6)
    assert len([m for m in hq.messages(t["id"]) if m.role == Role.agent]) == 5
