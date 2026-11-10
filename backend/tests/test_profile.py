"""Your profile: in every agent's instructions, and agents' suggestions for it."""

from __future__ import annotations

from app import learning, profile
from app.runtime import load_agent

from .test_learning import FAST, _reflections, _until


async def test_profile_goes_into_instructions(hq):
    await hq.start_worker()
    r = await hq.http.put("/api/profile", json={"text": "- Name: Mike\n- Prefers tables, failures first."})
    assert r.json()["words"] == 8 and r.json()["updated_at"]
    a = await hq.agent(name="Duke", model="fake:whoami")
    b = await hq.agent(name="Flint", model="fake:whoami", use_profile=False)
    ta, tb = await hq.thread(a["id"]), await hq.thread(b["id"])
    await hq.say(ta["id"], "who am I?")
    await hq.say(tb["id"], "who am I?")
    [ra] = await hq.wait_agent_replies(ta["id"], 1)
    [rb] = await hq.wait_agent_replies(tb["id"], 1)
    assert "Prefers tables, failures first." in ra and "Name: Mike" in ra
    assert rb == "no profile"  # this agent opted out
    await hq.http.put("/api/profile", json={"enabled": False})
    await hq.say(ta["id"], "and now?")
    assert (await hq.wait_agent_replies(ta["id"], 2))[-1] == "no profile"


def test_profile_is_cut_at_the_word_cap():
    text = "\n".join(f"- line {i} has five words" for i in range(40))
    out = profile.clipped(text, 52)
    assert out.endswith("…") and "line 7 " in out and "line 8 " not in out  # six words a line, dash included
    assert profile.clipped("short", 50) == "short"


async def test_a_standing_rule_becomes_a_profile_suggestion(hq):
    await hq.http.put("/api/learning/settings", json=FAST)
    await hq.start_worker()
    a = await hq.agent(name="Duke", model="fake:echo")
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "check the backups")
    await hq.wait_agent_replies(t["id"], 1)
    await hq.say(t["id"], "From now on, list failures first")
    await hq.wait_agent_replies(t["id"], 2)
    await _until(_reflections)
    await learning.run_due(hq.runtime)
    [p] = (await hq.http.get("/api/profile-proposals?status=pending")).json()
    assert p["text"] == "List failures first." and p["agent_name"] == "Duke"
    assert (await hq.http.get("/api/inbox/count")).json()["profile"] == 1
    msg = next(m for m in hq.messages(t["id"]) if m.meta.get("kind") == "profile_proposal")
    assert msg.meta["profile_proposal_id"] == p["id"]
    # The same suggestion isn't made twice while it waits.
    assert await hq.runtime.suggest_profile(load_agent(a["id"]), "list failures first", "again",
                                            thread_id=t["id"]) is None
    r = await hq.http.post(f"/api/profile-proposals/{p['id']}/decide",
                           json={"approve": True, "text": "List failures first, then the rest."})
    assert r.json()["status"] == "approved"
    assert profile.get()["text"] == "- List failures first, then the rest."
    assert next(m for m in hq.messages(t["id"]) if m.id == msg.id).meta["status"] == "approved"
    assert (await hq.http.post(f"/api/profile-proposals/{p['id']}/decide", json={"approve": False})).status_code == 409
    # Already in the profile: not suggested again.
    assert profile.already_known("list failures first, then the rest")
    assert (await hq.http.get("/api/inbox/count")).json()["profile"] == 0


async def test_suggestions_can_be_turned_off(hq):
    await hq.http.put("/api/learning/settings", json=FAST)
    await hq.http.put("/api/profile", json={"suggestions": False})
    await hq.start_worker()
    a = await hq.agent(name="Duke", model="fake:echo")
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "hi")
    await hq.wait_agent_replies(t["id"], 1)
    await hq.say(t["id"], "From now on, be brief")
    await hq.wait_agent_replies(t["id"], 2)
    await _until(_reflections)
    [r] = await learning.run_due(hq.runtime)
    assert r.status == "done" and r.profile_proposal_ids == []
    assert (await hq.http.get("/api/profile-proposals")).json() == []
