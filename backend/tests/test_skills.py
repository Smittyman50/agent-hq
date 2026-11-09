"""Skills: listing and loading, proposals under each learning mode, outcomes, versions."""

from __future__ import annotations

from sqlmodel import select

from app import db, skills
from app.models import SkillUse

BACKUPS = {"name": "check_pve_backups", "title": "Check Proxmox backups",
           "when_to_use": "When asked whether last night's Proxmox backups worked.",
           "body": "1. Open PBS on rewind.\n2. Check the TrueNAS datastore's last job.\n3. Report failures first."}


async def test_agents_list_load_and_follow_skills_and_outcomes_are_recorded(hq):
    await hq.start_worker()
    k = (await hq.http.post("/api/skills", json=BACKUPS)).json()
    assert k["version"] == 1 and k["versions"][0]["note"] == "created" and k["created_by"] == "tester"
    a = await hq.agent(name="Duke", model="fake:skilled")
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "did the backups run last night?")
    [reply] = await hq.wait_agent_replies(t["id"], 1)
    assert reply.startswith("Following check_pve_backups v1: 1. Open PBS on rewind.")
    with db.session() as s:
        [use] = s.exec(select(SkillUse)).all()
    assert use.outcome == "done" and use.version == 1
    listed = (await hq.http.get("/api/skills")).json()[0]
    assert listed["stats"]["uses"] == 1 and listed["success_rate"] == 1.0


async def test_retired_and_other_agents_skills_are_not_listed(hq):
    await hq.start_worker()
    other = await hq.agent(name="Scarlett", model="fake:worker")
    k = (await hq.http.post("/api/skills", json={**BACKUPS, "scope": "agent", "agent_id": other["id"]})).json()
    a = await hq.agent(name="Duke", model="fake:skilled")
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "backups?")
    assert (await hq.wait_agent_replies(t["id"], 1))[0].startswith("skill tools: ")
    await hq.http.put(f"/api/skills/{k['id']}", json={**BACKUPS, "scope": "team"})
    await hq.http.post(f"/api/skills/{k['id']}/status", json={"status": "retired"})
    await hq.say(t["id"], "backups?")
    assert (await hq.wait_agent_replies(t["id"], 2))[1].startswith("skill tools: ")


async def test_matching_lists_only_relevant_skills_when_there_are_many(hq):
    for i in range(14):
        await hq.http.post("/api/skills", json={**BACKUPS, "name": f"filler_{i:02d}", "title": f"Filler {i}",
                                                "when_to_use": f"When asked about gardening topic {i}."})
    await hq.http.post("/api/skills", json=BACKUPS)
    a = await hq.agent(name="Duke", model="fake:worker")
    from app.runtime import load_agent
    got = await skills.relevant(load_agent(a["id"]), "did the proxmox backups work?")
    assert [k.name for k in got][0] == "check_pve_backups" and len(got) < 15


async def test_propose_mode_sends_proposals_for_review(hq, monkeypatch):
    from app import subagents
    sent = []

    async def fake_notify(e):
        sent.append(e)
        return True

    monkeypatch.setattr(subagents, "notify", fake_notify)
    await hq.start_worker()
    a = await hq.agent(name="Duke", model="fake:proposer")
    assert a["learning_mode"] == "propose"  # the default
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "check the backups")
    assert (await hq.wait_agent_replies(t["id"], 1))[0].startswith("pending: Sent to the user for review")
    assert (await hq.http.get("/api/skills")).json() == []
    [p] = (await hq.http.get("/api/skill-proposals?status=pending")).json()
    assert p["name"] == "check_pve_backups" and p["agent_name"] == "Duke" and "diff" not in p
    assert (await hq.http.get("/api/inbox/count")).json()["skills"] == 1
    assert [m for m in hq.messages(t["id"]) if m.meta.get("kind") == "skill_proposal"]
    assert sent[0]["type"] == "skill_proposal"
    r = await hq.http.post(f"/api/skill-proposals/{p['id']}/decide",
                           json={"approve": True, "title": "Check Proxmox backups (PBS)"})
    assert r.json()["status"] == "approved"
    [k] = (await hq.http.get("/api/skills")).json()
    assert k["title"] == "Check Proxmox backups (PBS)" and k["created_by"] == "tester"
    detail = (await hq.http.get(f"/api/skills/{k['id']}")).json()
    assert detail["versions"][0]["note"].startswith("proposed by Duke: I worked this out")
    again = await hq.http.post(f"/api/skill-proposals/{p['id']}/decide", json={"approve": False})
    assert again.status_code == 409


async def test_revision_proposals_show_a_diff_and_make_a_new_version(hq):
    await hq.start_worker()
    k = (await hq.http.post("/api/skills", json=BACKUPS)).json()
    a = await hq.agent(name="Duke", model="fake:reviser")
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "backups again")
    await hq.wait_agent_replies(t["id"], 1)
    [p] = (await hq.http.get("/api/skill-proposals?status=pending")).json()
    assert p["skill_id"] == k["id"] and p["base_version"] == 1 and not p["stale"]
    assert "+2. Check the last job AND verify job." in p["diff"]
    await hq.http.post(f"/api/skill-proposals/{p['id']}/decide", json={"approve": True})
    k2 = (await hq.http.get(f"/api/skills/{k['id']}")).json()
    assert k2["version"] == 2 and "verify job" in k2["body"]
    # Roll back to version 1: a new version 3 with version 1's text.
    k3 = (await hq.http.post(f"/api/skills/{k['id']}/rollback", json={"version": 1})).json()
    assert k3["version"] == 3 and k3["body"] == BACKUPS["body"]
    assert k3["versions"][0]["note"] == "rolled back to version 1"


async def test_auto_and_off_learning_modes(hq):
    await hq.start_worker()
    a = await hq.agent(name="Duke", model="fake:proposer", learning_mode="auto")
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "check the backups")
    assert (await hq.wait_agent_replies(t["id"], 1))[0] == "applied: Saved check_pve_backups (version 1)."
    assert [k["name"] for k in (await hq.http.get("/api/skills")).json()] == ["check_pve_backups"]
    [p] = (await hq.http.get("/api/skill-proposals")).json()
    assert p["status"] == "applied"

    b = await hq.agent(name="Scarlett", model="fake:proposer", learning_mode="off")
    t2 = await hq.thread(b["id"])
    await hq.say(t2["id"], "anything")
    assert await hq.wait_agent_replies(t2["id"], 1) == ["I can't propose skills."]


async def test_bad_skills_are_rejected(hq):
    bad = await hq.http.post("/api/skills", json={**BACKUPS, "name": "Check Backups!"})
    assert bad.status_code == 422
    await hq.http.post("/api/skills", json=BACKUPS)
    dup = await hq.http.post("/api/skills", json=BACKUPS)
    assert dup.status_code == 409
    nobody = await hq.http.post("/api/skills", json={**BACKUPS, "name": "mine", "scope": "agent"})
    assert nobody.status_code == 422
