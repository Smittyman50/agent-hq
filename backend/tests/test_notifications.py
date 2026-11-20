"""Notification channels: Telegram end to end against a fake Bot API, routing rules, the
MQTT channel, pairing, decisions from a chat, replies into conversations and alerts."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from sqlmodel import select

from app import db, notifications
from app.config import reload_settings
from app.models import (ChannelInbound, Notice, NotificationChannel, Role, Thread, ToolApproval)

from .fake_telegram import GOOD_TOKEN, FakeTelegram

WB_DIR = Path(__file__).resolve().parents[2] / "workbench"


@pytest.fixture
async def tg(hq, monkeypatch):
    fake = FakeTelegram()
    fake.start()
    monkeypatch.setenv("AHQ_TELEGRAM_API_URL", fake.url)
    monkeypatch.setenv("AHQ_PUBLIC_URL", "http://hq.test")
    reload_settings()
    listeners = notifications.Listeners(hq.runtime, refresh=0.1)
    task = asyncio.create_task(listeners.run())
    fake.listeners = listeners
    yield fake
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    fake.stop()


async def _until(check, timeout: float = 15):
    for _ in range(int(timeout * 20)):
        got = check()
        if got:
            return got
        await asyncio.sleep(0.05)
    raise AssertionError("condition never held")


def _channel(cid: str) -> NotificationChannel:
    with db.session() as s:
        return s.get(NotificationChannel, cid)


async def _paired(hq, tg, user_id=42, **body) -> dict:
    r = await hq.http.post("/api/notifications/channels", json={
        "name": body.pop("name", "Phone"), "kind": "telegram", "token": GOOD_TOKEN, **body})
    assert r.status_code == 201, r.text
    ch = r.json()
    code = (await hq.http.post(f"/api/notifications/channels/{ch['id']}/pair")).json()
    assert code["link"] == f"https://t.me/hq_test_bot?start={code['code']}"
    n = len(tg.sent())
    tg.push(tg.user_message(f"/start {code['code']}", user_id=user_id))
    await _until(lambda: _channel(ch["id"]).paired)
    assert "Paired with Agent HQ" in (await tg.wait_sent(n + 1))[-1]["text"]
    return ch


def _last_to(tg, chat_id=42) -> dict:
    return [m for m in tg.sent() if str(m["chat_id"]) == str(chat_id)][-1]


async def test_channel_checks_the_token_and_never_shows_it(hq, tg, monkeypatch):
    monkeypatch.setenv("AHQ_SECRET_KEY", "test-secret-key-long-enough-for-fernet-derivation")
    reload_settings()
    bad = await hq.http.post("/api/notifications/channels",
                             json={"name": "Phone", "kind": "telegram", "token": "nope"})
    assert bad.status_code == 422 and "Unauthorized" in bad.text
    r = await hq.http.post("/api/notifications/channels",
                           json={"name": "Phone", "kind": "telegram", "token": GOOD_TOKEN})
    ch = r.json()
    assert r.status_code == 201 and ch["bot_username"] == "hq_test_bot" and ch["has_token"]
    assert GOOD_TOKEN not in r.text
    assert set(ch["routes"][0]["types"]) == set(notifications.DEFAULT_CHAT_TYPES)
    stored = _channel(ch["id"]).config["token"]
    assert stored != GOOD_TOKEN and stored.startswith("enc:")
    listed = await hq.http.get("/api/notifications/channels")
    assert GOOD_TOKEN not in listed.text
    # The test button needs someone paired first.
    assert (await hq.http.post(f"/api/notifications/channels/{ch['id']}/test")).status_code == 409


async def test_pairing_and_strangers(hq, tg):
    ch = await _paired(hq, tg)
    assert _channel(ch["id"]).paired[0]["user_id"] == "42"
    n = len(tg.sent())
    # Someone else: a wrong code, a chat, a button. Nothing happens and nothing is said back.
    tg.push(tg.user_message("/start WRONG", user_id=7))
    tg.push(tg.user_message("hello?", user_id=7))
    await _until(lambda: len(_inbound(ch["id"], "refused")) == 2)
    await asyncio.sleep(0.3)
    assert len(tg.sent()) == n
    # The test notice reaches the paired chat.
    r = await hq.http.post(f"/api/notifications/channels/{ch['id']}/test")
    assert r.status_code == 200, r.text
    assert "Test from Agent HQ" in _last_to(tg)["text"]


def _inbound(cid: str, kind: str) -> list[ChannelInbound]:
    with db.session() as s:
        return list(s.exec(select(ChannelInbound).where(ChannelInbound.channel_id == cid,
                                                        ChannelInbound.kind == kind)).all())


async def test_mqtt_channel_is_set_up_once_from_the_old_setting(hq):
    notifications.ensure_defaults()
    chans = (await hq.http.get("/api/notifications/channels")).json()
    [mqtt] = [c for c in chans if c["kind"] == "mqtt"]
    assert mqtt["enabled"] is False  # no AHQ_MQTT_URL in the tests
    assert mqtt["routes"][0]["types"] == [] and mqtt["decisions_allowed"] is False
    await hq.http.delete(f"/api/notifications/channels/{mqtt['id']}")
    notifications.ensure_defaults()
    assert not [c for c in (await hq.http.get("/api/notifications/channels")).json() if c["kind"] == "mqtt"]


async def test_mqtt_channel_publishes_the_same_json(hq, monkeypatch):
    import json
    import socket

    import aiomqtt
    from amqtt.broker import Broker as MqttBroker

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    broker = MqttBroker({"listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{port}"}},
                         "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}}})
    await broker.start()
    try:
        monkeypatch.setenv("AHQ_MQTT_URL", f"mqtt://127.0.0.1:{port}")
        reload_settings()
        notifications.ensure_defaults()
        got = []
        async with aiomqtt.Client("127.0.0.1", port) as sub:
            await sub.subscribe("agent-hq/notifications", qos=1)
            from app import subagents

            assert await subagents.notify({"type": "goal_blocked", "goal_id": "g1", "title": "Backups",
                                           "agent": "Mainframe", "reason": "turn limit"})
            async with asyncio.timeout(10):
                async for msg in sub.messages:
                    got.append(json.loads(msg.payload))
                    break
        assert got[0]["type"] == "goal_blocked" and got[0]["reason"] == "turn limit" and "at" in got[0]
    finally:
        await broker.shutdown()


async def test_routing_by_type_agent_and_severity(hq, tg):
    breaker = await hq.agent(name="Breaker", model="fake:echo")
    await hq.agent(name="Mainframe", model="fake:echo")
    ch = await _paired(hq, tg)
    r = await hq.http.put(f"/api/notifications/channels/{ch['id']}", json={
        "name": "Phone", "kind": "telegram", "routes": [
            {"types": ["agent_alert"], "agent_ids": [breaker["id"]], "min_severity": "warning"}]})
    assert r.status_code == 200, r.text
    n = len(tg.sent())
    for agent, sev in (("Mainframe", "critical"), ("Breaker", "info"), ("Breaker", "critical")):
        await notifications.dispatch({"type": "agent_alert", "agent": agent, "severity": sev, "text": f"{agent} {sev}"})
    await notifications.dispatch({"type": "goal_blocked", "agent": "Breaker", "title": "x", "reason": "y"})
    sent = tg.sent()[n:]
    assert len(sent) == 1 and "Breaker critical" in sent[0]["text"] and sent[0]["text"].startswith("<b>🚨 Breaker")
    bad = await hq.http.put(f"/api/notifications/channels/{ch['id']}", json={
        "name": "Phone", "kind": "telegram", "routes": [{"types": ["nonsense"]}]})
    assert bad.status_code == 422


async def _approval_setup(hq, workdir):
    launch = ("import sys; sys.path.insert(0, %r); import workbench_mcp as w; "
              "w.WORK.mkdir(parents=True, exist_ok=True); w.ensure_venv(); w.mcp.run()") % str(WB_DIR)
    srv = (await hq.http.post("/api/tool-servers", json={
        "name": "bench", "transport": "stdio", "command": sys.executable, "args": ["-c", launch],
        "env": {"WORKBENCH_DIR": str(workdir)}})).json()
    a = await hq.agent(name="Flint", model="fake:coder", tool_server_ids=[srv["id"]])
    return a, await hq.thread(a["id"])


async def test_tool_approval_from_telegram(hq, tg, tmp_path):
    await _paired(hq, tg)
    await hq.start_worker()
    _, t = await _approval_setup(hq, tmp_path / "bench")
    n = len(tg.sent())
    await hq.say(t["id"], "what is 6*7?")
    [msg] = (await tg.wait_sent(n + 1))[n:]
    assert "Flint wants to run run_python on bench" in msg["text"] and "print(6*7)" in msg["text"]
    buttons = msg["reply_markup"]["inline_keyboard"][0]
    assert [b["text"] for b in buttons] == ["✅ Approve", "❌ Deny"]
    tg.push(tg.tap(msg, buttons[0]["callback_data"]))
    answer = (await tg.wait_sent(1, "answerCallbackQuery"))[-1]
    assert answer["text"] == "Approved."
    assert await hq.wait_agent_replies(t["id"], 1) == ["42"]
    edited = (await tg.wait_sent(1, "editMessageText"))[-1]
    assert edited["message_id"] == msg["message_id"] and "✅ Approved via Phone (Mike)" in edited["text"]
    assert "reply_markup" not in edited
    with db.session() as s:
        assert s.exec(select(ToolApproval)).one().status == "approved"
    # You answered from the chat, so the agent's answer follows you there.
    await _until(lambda: [m for m in tg.sent() if m["text"] == "<b>Flint</b>\n42"])
    # A second tap is told it's already decided.
    tg.push(tg.tap(msg, buttons[1]["callback_data"]))
    await _until(lambda: len(tg.sent("answerCallbackQuery")) == 2)
    assert "Approved via Phone" in tg.sent("answerCallbackQuery")[-1]["text"]


async def test_ui_decision_settles_the_chat_message_and_deny_by_reply(hq, tg, tmp_path):
    await _paired(hq, tg)
    await hq.start_worker()
    _, t = await _approval_setup(hq, tmp_path / "bench")
    n = len(tg.sent())
    await hq.say(t["id"], "what is 6*7?")
    [msg] = (await tg.wait_sent(n + 1))[n:]
    # A reply that isn't a decision gets a hint, and nothing is decided.
    tg.push(tg.user_message("hmm", reply_to=msg["message_id"]))
    await _until(lambda: "Use the buttons" in _last_to(tg)["text"])
    # "no, ..." denies with the rest as the note.
    tg.push(tg.user_message("no, not on a Friday", reply_to=msg["message_id"]))
    [reply] = await hq.wait_agent_replies(t["id"], 1)
    assert "denied" in reply and "not on a Friday" in reply
    with db.session() as s:
        a = s.exec(select(ToolApproval)).one()
    assert a.status == "denied" and a.note == "not on a Friday"
    # The next one is decided in the web UI: the chat message is edited all the same.
    await hq.say(t["id"], "again")
    (await tg.wait_sent(1, "editMessageText"))
    m2 = await _until(lambda: [m for m in tg.sent() if "wants to run" in m["text"]][1:])
    req = (await _until(lambda: _pending_approvals()))[0]
    r = await hq.http.post(f"/api/tool-approvals/{req.id}/decide", json={"approve": True})
    assert r.status_code == 200
    edits = await _until(lambda: [e for e in tg.sent("editMessageText") if e["message_id"] == m2[0]["message_id"]])
    assert "✅ Approved in Agent HQ" in edits[-1]["text"]


def _pending_approvals():
    with db.session() as s:
        return list(s.exec(select(ToolApproval).where(ToolApproval.status == "pending")).all())


async def test_decisions_off_sends_a_link_only(hq, tg, tmp_path):
    await _paired(hq, tg, decisions_allowed=False)
    await hq.start_worker()
    _, t = await _approval_setup(hq, tmp_path / "bench")
    n = len(tg.sent())
    await hq.say(t["id"], "what is 6*7?")
    [msg] = (await tg.wait_sent(n + 1))[n:]
    assert "reply_markup" not in msg and "Decide in Agent HQ: http://hq.test/requests/approvals" in msg["text"]
    tg.push(tg.user_message("yes", reply_to=msg["message_id"]))
    await _until(lambda: "Decisions are turned off" in _last_to(tg)["text"])
    assert _pending_approvals()
    await hq.http.post(f"/api/tool-approvals/{_pending_approvals()[0].id}/decide", json={"approve": False})
    await hq.wait_agent_replies(t["id"], 1)


async def test_chat_with_the_default_agent_and_reply_to_continue(hq, tg):
    duke = await hq.agent(name="Duke", model="fake:echo")
    ch = await _paired(hq, tg, default_agent_id=duke["id"])
    await hq.start_worker()
    tg.push(tg.user_message("status of the lab?"))
    reply = await _until(lambda: [m for m in tg.sent() if "You said: status of the lab?" in m["text"]])
    assert reply[0]["text"].startswith("<b>Duke</b>")
    assert tg.sent("setMessageReaction")  # 👍 on your message: it was taken
    tid = _channel(ch["id"]).chat_thread_id
    with db.session() as s:
        assert s.get(Thread, tid).agent_id == duke["id"]
    # Replying to the agent's message continues that conversation.
    tg.push(tg.user_message("and the NAS?", reply_to=reply[0]["message_id"]))
    await _until(lambda: [m for m in tg.sent() if "You said: and the NAS?" in m["text"]])
    users = [m.content for m in hq.messages(tid) if m.role == Role.user]
    assert users == ["status of the lab?", "and the NAS?"]
    # /new starts another conversation.
    tg.push(tg.user_message("/new"))
    await _until(lambda: _channel(ch["id"]).chat_thread_id != tid)


async def test_alert_tool_reaches_the_phone_and_a_reply_continues_it(hq, tg):
    await _paired(hq, tg)
    a = await hq.agent(name="Breaker", model="fake:alerter", can_alert=True)
    quiet = await hq.agent(name="Quiet", model="fake:alerter")
    await hq.start_worker()
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "gateway 1")
    assert await hq.wait_agent_replies(t["id"], 1) == ["alert sent"]
    alert = [m for m in tg.sent() if "WAN down" in m["text"]][0]
    assert alert["text"].startswith("<b>🚨 Breaker</b>") and f"http://hq.test/chat/{t['id']}" in alert["text"]
    assert any(m.meta.get("kind") == "agent_alert" for m in hq.messages(t["id"]))
    # Your reply lands in Breaker's conversation, and its answer comes back.
    tg.push(tg.user_message("reboot it?", reply_to=alert["message_id"]))
    await hq.wait_agent_replies(t["id"], 2)
    assert [m.content for m in hq.messages(t["id"]) if m.role == Role.user][-1] == "reboot it?"
    # Agents without the setting don't get the tool.
    t2 = await hq.thread(quiet["id"])
    await hq.say(t2["id"], "x")
    assert await hq.wait_agent_replies(t2["id"], 1) == ["I can't send alerts."]


async def test_alerts_are_rate_limited(hq, tg):
    await _paired(hq, tg)
    notifications.save_settings({"alerts_per_hour": 1})
    a = await hq.agent(name="Breaker", model="fake:alerter", can_alert=True)
    await hq.start_worker()
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "one")
    await hq.wait_agent_replies(t["id"], 1)
    await hq.say(t["id"], "two")
    assert (await hq.wait_agent_replies(t["id"], 2))[-1] == "alert refused"


async def test_reply_to_a_goal_question_goes_to_the_goal(hq, tg):
    duke = await hq.agent(name="Duke", model="fake:echo")
    await _paired(hq, tg)
    await hq.start_worker()
    t = await hq.thread(duke["id"])
    await notifications.dispatch({"type": "goal_question", "agent": "Duke", "title": "Backups",
                                  "question": "Which datastore?", "thread_id": t["id"]})
    q = [m for m in tg.sent() if "Duke needs you: Backups" in m["text"]][0]
    assert "Which datastore?" in q["text"] and "reply_markup" not in q
    tg.push(tg.user_message("the TrueNAS one", reply_to=q["message_id"]))
    assert await hq.wait_agent_replies(t["id"], 1) == ["You said: the TrueNAS one"]
    await _until(lambda: [m for m in tg.sent() if "You said: the TrueNAS one" in m["text"]])


async def test_only_one_listener_reads_a_bot(hq, tg):
    await _paired(hq, tg)
    second = notifications.Listeners(hq.runtime, refresh=0.1)
    task = asyncio.create_task(second.run())
    try:
        await asyncio.sleep(0.6)
        held = [t for _, t in second.tasks.values()]
        assert held and not any(t.done() for t in held)
        # The second one is waiting for the lock, not polling: updates are handled once.
        tg.push(tg.user_message("/help"))
        await _until(lambda: [m for m in tg.sent() if m["text"].startswith("Agent HQ")])
        await asyncio.sleep(0.3)
        assert len([m for m in tg.sent() if m["text"].startswith("Agent HQ")]) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_delivery_log(hq, tg):
    ch = await _paired(hq, tg)
    await hq.http.post(f"/api/notifications/channels/{ch['id']}/test")
    rows = (await hq.http.get(f"/api/notifications/channels/{ch['id']}/log")).json()
    assert rows[0]["direction"] == "out" and rows[0]["kind"] == "test" and rows[0]["status"] == "sent"
    assert any(r["direction"] == "in" and r["kind"] == "pairing" for r in rows)


async def test_old_notices_are_pruned(hq, tg):
    from datetime import datetime, timedelta, timezone

    from app.models import NoticeDelivery

    ch = await _paired(hq, tg)
    await hq.http.post(f"/api/notifications/channels/{ch['id']}/test")
    with db.session() as s:
        for n in s.exec(select(Notice)).all():
            n.created_at = datetime.now(timezone.utc) - timedelta(days=40)
            s.add(n)
        s.commit()
    assert notifications.prune() == 1
    with db.session() as s:
        assert not s.exec(select(Notice)).all() and not s.exec(select(NoticeDelivery)).all()


async def test_deleting_an_agent_cleans_up_its_channels(hq, tg):
    duke = await hq.agent(name="Duke", model="fake:echo")
    ch = await _paired(hq, tg, default_agent_id=duke["id"], routes=[
        {"types": ["agent_alert"], "agent_ids": [duke["id"]]}, {"types": ["tool_approval"]}])
    await hq.http.put(f"/api/agents/{duke['id']}", json={**duke, "enabled": False})
    r = await hq.http.delete(f"/api/agents/{duke['id']}/permanent")
    assert r.status_code == 204, r.text
    row = _channel(ch["id"])
    assert row.default_agent_id is None and row.chat_thread_id is None
    rules = (await hq.http.get("/api/notifications/channels")).json()[-1]["routes"]
    # The rule that named only Duke is off, not widened to every agent.
    assert [(r["types"], r["agent_ids"], r["enabled"]) for r in rules] == [
        (["agent_alert"], [], False), (["tool_approval"], [], True)]


def test_the_bot_token_never_reaches_the_logs(caplog):
    import logging

    import app.channels.telegram  # noqa: F401  (installs the filter)

    with caplog.at_level(logging.INFO, logger="httpx"):
        logging.getLogger("httpx").info('HTTP Request: POST %s "HTTP/1.1 200 OK"',
                                        "https://api.telegram.org/bot123456:AAH-secret_x/getUpdates")
    assert "AAH-secret_x" not in caplog.text and "/bot<token>/getUpdates" in caplog.text
