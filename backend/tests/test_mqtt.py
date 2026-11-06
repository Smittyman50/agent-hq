from __future__ import annotations

import asyncio
import socket

import aiomqtt
import pytest

from app.mqtt_bridge import MqttBridge, topic_matches


@pytest.mark.parametrize("flt,topic,ok", [
    ("homelab/alerts/#", "homelab/alerts/opnsense/wan", True),
    ("homelab/alerts/#", "homelab/alerts", True),
    ("homelab/+/cpu", "homelab/pve1/cpu", True),
    ("homelab/+/cpu", "homelab/pve1/mem", False),
    ("homelab/+/cpu", "homelab/pve1/x/cpu", False),
    ("a/b", "a/b/c", False),
])
def test_topic_matching(flt, topic, ok):
    assert topic_matches(flt, topic) is ok


async def _alert_setup(hq, interval=60):
    await hq.start_worker()
    a = await hq.agent(name="Watcher", model="fake:alert")
    r = await hq.http.post("/api/subscriptions", json={
        "name": "alerts", "topic_filter": "homelab/alerts/#", "agent_id": a["id"],
        "min_interval_seconds": interval})
    assert r.status_code == 201, r.text
    return a, r.json()


async def test_injected_event_wakes_agent_in_its_own_thread(hq):
    _, sub = await _alert_setup(hq)
    r = await hq.http.post("/api/subscriptions/test",
                           json={"topic": "homelab/alerts/opnsense", "payload": "WAN down"})
    assert len(r.json()["queued_jobs"]) == 1
    thread_id = (await hq.http.get("/api/subscriptions")).json()[0]["thread_id"]
    assert await hq.wait_agent_replies(thread_id, 1) == ["Handled event: WAN down"]
    # Cooldown: a second alert inside the interval is ignored.
    r = await hq.http.post("/api/subscriptions/test",
                           json={"topic": "homelab/alerts/opnsense", "payload": "WAN still down"})
    assert r.json()["queued_jobs"] == []
    # Non-matching topics are ignored.
    r = await hq.http.post("/api/subscriptions/test", json={"topic": "other/x", "payload": "x"})
    assert r.json()["queued_jobs"] == []


async def test_real_broker_end_to_end(hq):
    """amqtt broker -> MqttBridge -> worker -> agent reply."""
    from amqtt.broker import Broker as MqttBroker

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    broker = MqttBroker({"listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{port}"}},
                         "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin":
                                     {"allow_anonymous": True}}})
    await broker.start()
    try:
        await _alert_setup(hq, interval=0)
        bridge = MqttBridge(hq.runtime, f"mqtt://127.0.0.1:{port}", refresh_seconds=0.2)
        task = asyncio.create_task(bridge.run())
        await asyncio.wait_for(bridge.connected.wait(), 10)
        async with aiomqtt.Client("127.0.0.1", port) as pub:
            await pub.publish("homelab/alerts/pve1", b"disk 95% full", qos=1)
        for _ in range(100):
            subs = (await hq.http.get("/api/subscriptions")).json()
            if subs[0]["thread_id"]:
                break
            await asyncio.sleep(0.05)
        replies = await hq.wait_agent_replies(subs[0]["thread_id"], 1)
        assert replies == ["Handled event: disk 95% full"]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    finally:
        await broker.shutdown()
