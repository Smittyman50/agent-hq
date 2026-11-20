"""The Discord channel end to end, against a fake Discord (REST and Gateway): the token
checked and hidden, pairing in a DM and in a server channel, buttons through interactions,
replies, the default agent's chat, the Message Content intent, and resuming the Gateway."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from sqlmodel import select

from app import db, notifications
from app.config import reload_settings
from app.models import ChannelInbound, NotificationChannel, Role, ToolApproval

from .fake_discord import APP_ID, GOOD_TOKEN, FakeDiscord

WB_DIR = Path(__file__).resolve().parents[2] / "workbench"


@pytest.fixture
async def dc(hq, monkeypatch):
    fake = FakeDiscord()
    fake.start()
    monkeypatch.setenv("AHQ_DISCORD_API_URL", fake.api)
    monkeypatch.setenv("AHQ_PUBLIC_URL", "http://hq.test")
    reload_settings()
    listeners = notifications.Listeners(hq.runtime, refresh=0.1)
    task = asyncio.create_task(listeners.run())
    yield fake
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    fake.stop()


def _channel(cid: str) -> NotificationChannel:
    with db.session() as s:
        return s.get(NotificationChannel, cid)


def _inbound(cid: str, kind: str) -> list[ChannelInbound]:
    with db.session() as s:
        return list(s.exec(select(ChannelInbound).where(ChannelInbound.channel_id == cid,
                                                        ChannelInbound.kind == kind)).all())


async def _create(hq, **body) -> dict:
    r = await hq.http.post("/api/notifications/channels", json={
        "name": body.pop("name", "Discord"), "kind": "discord", "token": GOOD_TOKEN, **body})
    assert r.status_code == 201, r.text
    return r.json()


async def _paired(hq, dc, **body) -> dict:
    ch = await _create(hq, **body)
    await dc.wait(lambda: dc.identified)
    code = (await hq.http.post(f"/api/notifications/channels/{ch['id']}/pair")).json()
    assert code["command"] == f"!start {code['code']}" and f"client_id={APP_ID}" in code["link"]
    dc.dm(f"!start {code['code']}")
    await dc.wait(lambda: _channel(ch["id"]).paired)
    await dc.wait(lambda: [m for m in dc.messages() if "Paired with Agent HQ" in m.get("content", "")])
    return ch


async def test_channel_checks_the_token_and_gives_an_invite_link(hq, dc, monkeypatch):
    monkeypatch.setenv("AHQ_SECRET_KEY", "test-secret-key-long-enough-for-fernet-derivation")
    reload_settings()
    bad = await hq.http.post("/api/notifications/channels",
                             json={"name": "Discord", "kind": "discord", "token": "nope"})
    assert bad.status_code == 422 and "rejected the bot token" in bad.text
    ch = await _create(hq)
    assert ch["bot_username"] == "agenthq" and ch["has_token"] and GOOD_TOKEN not in str(ch)
    assert ch["invite_link"].startswith(f"https://discord.com/oauth2/authorize?client_id={APP_ID}&scope=bot")
    assert _channel(ch["id"]).config["token"].startswith("enc:")
    # The listener connects with the token and the unprivileged intents only.
    [ident] = await dc.wait(lambda: dc.identified)
    assert ident["token"] == GOOD_TOKEN and ident["intents"] == (1 << 9) | (1 << 12)


async def test_pairing_by_dm_and_strangers_ignored(hq, dc):
    ch = await _paired(hq, dc)
    assert _channel(ch["id"]).paired[0] | {"paired_at": None} == {
        "user_id": "4242", "chat_id": "dm-4242", "name": "Mike", "paired_at": None}
    n = len(dc.messages())
    stranger = {"id": "7", "username": "eve"}
    dc.dm("!start WRONG", user=stranger, channel="dm-7")
    dc.dm("hi there", user=stranger, channel="dm-7")
    await dc.wait(lambda: len(_inbound(ch["id"], "refused")) == 2)
    await asyncio.sleep(0.3)
    assert len(dc.messages()) == n
    r = await hq.http.post(f"/api/notifications/channels/{ch['id']}/test")
    assert r.status_code == 200, r.text
    [embed] = dc.messages()[-1]["embeds"]
    assert embed["title"] == "Test from Agent HQ" and dc.messages()[-1]["_path"] == "/channels/dm-4242/messages"


async def _mid(dc, notice_type: str, chat: str = "dm-4242") -> str:
    """The Discord message ID the latest notice of this type got (once its delivery is saved)."""
    from app.models import Notice, NoticeDelivery

    def find():
        with db.session() as s:
            rows = s.exec(select(NoticeDelivery, Notice).where(
                NoticeDelivery.notice_id == Notice.id, Notice.type == notice_type,
                NoticeDelivery.chat_id == chat, NoticeDelivery.message_id != "")).all()
        return rows[-1][0].message_id if rows else None
    return await dc.wait(find)


async def _approval_setup(hq, workdir):
    launch = ("import sys; sys.path.insert(0, %r); import workbench_mcp as w; "
              "w.WORK.mkdir(parents=True, exist_ok=True); w.ensure_venv(); w.mcp.run()") % str(WB_DIR)
    srv = (await hq.http.post("/api/tool-servers", json={
        "name": "bench", "transport": "stdio", "command": sys.executable, "args": ["-c", launch],
        "env": {"WORKBENCH_DIR": str(workdir)}})).json()
    a = await hq.agent(name="Flint", model="fake:coder", tool_server_ids=[srv["id"]])
    return a, await hq.thread(a["id"])


def _requests(dc) -> list[dict]:
    return [m for m in dc.messages() if m.get("embeds") and "wants to run" in m["embeds"][0]["title"]]


async def test_tool_approval_by_button(hq, dc, tmp_path):
    await _paired(hq, dc)
    await hq.start_worker()
    _, t = await _approval_setup(hq, tmp_path / "bench")
    await hq.say(t["id"], "what is 6*7?")
    [msg] = await dc.wait(lambda: _requests(dc))
    assert "print(6*7)" in msg["embeds"][0]["description"]
    buttons = msg["components"][0]["components"]
    assert [(b["label"], b["style"]) for b in buttons] == [("✅ Approve", 3), ("❌ Deny", 4)]
    mid = await _mid(dc, "tool_approval")
    iid = dc.press(mid, buttons[0]["custom_id"])
    # Deferred at once (only the presser sees it), then filled in with the outcome.
    await dc.wait(lambda: dc.posted("POST", f"/interactions/{iid}/"))
    assert dc.posted("POST", f"/interactions/{iid}/")[0][1] == {"type": 5, "data": {"flags": 64}}
    [(_, answer)] = await dc.wait(lambda: dc.posted("PATCH", f"/webhooks/{APP_ID}/tok{iid}/"))
    assert answer["content"] == "Approved."
    assert await hq.wait_agent_replies(t["id"], 1) == ["42"]
    [(_, edit)] = await dc.wait(lambda: dc.posted("PATCH", f"/channels/dm-4242/messages/{mid}"))
    assert edit["components"] == [] and "✅ Approved via Discord (Mike)" in edit["embeds"][0]["description"]
    with db.session() as s:
        assert s.exec(select(ToolApproval)).one().status == "approved"
    # You answered from Discord, so the agent's answer follows you there, as plain text.
    await dc.wait(lambda: [m for m in dc.messages() if m.get("content") == "**Flint**\n42"])
    assert all(m["allowed_mentions"] == {"parse": []} for m in dc.messages())


async def test_deny_by_reply_with_a_note(hq, dc, tmp_path):
    await _paired(hq, dc)
    await hq.start_worker()
    _, t = await _approval_setup(hq, tmp_path / "bench")
    await hq.say(t["id"], "what is 6*7?")
    mid = await _mid(dc, "tool_approval")
    dc.dm("no, not on a Friday", reply_to=mid)
    [reply] = await hq.wait_agent_replies(t["id"], 1)
    assert "denied" in reply and "not on a Friday" in reply


async def test_chat_with_the_default_agent(hq, dc):
    duke = await hq.agent(name="Duke", model="fake:echo")
    ch = await _paired(hq, dc, default_agent_id=duke["id"])
    await hq.start_worker()
    dc.dm("status of the lab?")
    [got] = await dc.wait(lambda: [m for m in dc.messages() if "You said: status of the lab?" in m.get("content", "")])
    assert got["content"].startswith("**Duke**\n")
    await dc.wait(lambda: dc.posted("PUT", "/channels/dm-4242/messages/"))  # 👍 on your message
    tid = _channel(ch["id"]).chat_thread_id
    mid = await _mid(dc, "agent_reply")
    dc.dm("and the NAS?", reply_to=mid)
    await dc.wait(lambda: [m for m in dc.messages() if "You said: and the NAS?" in m.get("content", "")])
    assert [m.content for m in hq.messages(tid) if m.role == Role.user] == ["status of the lab?", "and the NAS?"]
    dc.dm("!new")
    await dc.wait(lambda: _channel(ch["id"]).chat_thread_id != tid)


async def test_server_channel_only_hears_what_is_addressed_to_it(hq, dc):
    duke = await hq.agent(name="Duke", model="fake:echo")
    ch = await _create(hq, default_agent_id=duke["id"])
    await dc.wait(lambda: dc.identified)
    await hq.start_worker()
    code = (await hq.http.post(f"/api/notifications/channels/{ch['id']}/pair")).json()["code"]
    # People talking in the channel: not for the bot, not logged.
    dc.dm("lunch?", channel="lab-ops", guild="g1", user={"id": "8", "username": "bob"})
    dc.dm(f"<@5550001> !start {code}", channel="lab-ops", guild="g1", mention=True)
    await dc.wait(lambda: _channel(ch["id"]).paired)
    assert _channel(ch["id"]).paired[0]["chat_id"] == "lab-ops" and not _inbound(ch["id"], "refused")
    # Without Message Content, a plain message in the channel shows up empty: ignored.
    dc.dm("status?", channel="lab-ops", guild="g1")
    await asyncio.sleep(0.4)
    assert not _inbound(ch["id"], "chat")
    # Mentioning the bot works, and notices now go to the channel.
    dc.dm("<@5550001> status?", channel="lab-ops", guild="g1", mention=True)
    await dc.wait(lambda: [m for m in dc.messages() if m["_path"] == "/channels/lab-ops/messages"
                           and "You said: status?" in m.get("content", "")])


async def test_message_content_needs_the_portal_switch(hq, dc):
    ch = await _create(hq)
    await dc.wait(lambda: dc.identified)
    r = await hq.http.put(f"/api/notifications/channels/{ch['id']}", json={
        "name": "Discord", "kind": "discord", "message_content": True})
    assert r.status_code == 200 and r.json()["message_content"] is True
    await dc.wait(lambda: "Developer Portal" in (_channel(ch["id"]).last_error or ""))
    # Turning it off here reconnects at once without it.
    before = len(dc.identified)
    await hq.http.put(f"/api/notifications/channels/{ch['id']}", json={
        "name": "Discord", "kind": "discord", "message_content": False})
    await dc.wait(lambda: len(dc.identified) > before)
    await dc.wait(lambda: not _channel(ch["id"]).last_error)
    # Or turn it on in the portal, then here.
    dc.content_allowed = True
    await hq.http.put(f"/api/notifications/channels/{ch['id']}", json={
        "name": "Discord", "kind": "discord", "message_content": True})
    await dc.wait(lambda: any(i["intents"] & (1 << 15) for i in dc.identified))


async def test_gateway_resumes_after_a_drop(hq, dc):
    duke = await hq.agent(name="Duke", model="fake:echo")
    await _paired(hq, dc, default_agent_id=duke["id"])
    await hq.start_worker()
    dc.drop_now = True
    await dc.wait(lambda: dc.resumed == 1, timeout=20)
    assert len(dc.identified) == 1  # resumed the session, not a new one
    dc.dm("still there?")
    await dc.wait(lambda: [m for m in dc.messages() if "You said: still there?" in m.get("content", "")])


async def test_routing_works_the_same_as_telegram(hq, dc):
    breaker = await hq.agent(name="Breaker", model="fake:echo")
    ch = await _paired(hq, dc)
    await hq.http.put(f"/api/notifications/channels/{ch['id']}", json={
        "name": "Discord", "kind": "discord",
        "routes": [{"types": ["agent_alert"], "agent_ids": [breaker["id"]], "min_severity": "critical"}]})
    n = len(dc.messages())
    await notifications.dispatch({"type": "agent_alert", "agent": "Breaker", "severity": "warning", "text": "meh"})
    await notifications.dispatch({"type": "agent_alert", "agent": "Breaker", "severity": "critical", "text": "WAN down"})
    [m] = dc.messages()[n:]
    assert m["embeds"][0]["title"] == "🚨 Breaker" and m["embeds"][0]["color"] == 0xEF4444
    assert m["components"] == []
