"""The MQTT publisher tool server (mqttpublisher/mqtt_publisher_mcp.py): status and allow-listed
publishes reach a real broker; wildcards, system, denied and off-list topics, oversized payloads
and bursts are refused; and an Agent HQ agent publishes after you approve it.

Runs the real server as a subprocess against an in-process amqtt broker.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import aiomqtt
import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart

from .conftest import SCRIPTS, _tool_returns

pytest.importorskip("fastmcp")
from fastmcp import Client  # noqa: E402

SERVER = Path(__file__).resolve().parents[2] / "mqttpublisher" / "mqtt_publisher_mcp.py"
sys.path.insert(0, str(SERVER.parent))
import mqtt_publisher_mcp as pub  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def broker():
    from amqtt.broker import Broker as MqttBroker

    port = _free_port()
    b = MqttBroker({"listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{port}"}},
                    "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}}})
    await b.start()
    yield port
    await b.shutdown()


def _start(env: dict) -> tuple[subprocess.Popen, str]:
    port = _free_port()
    proc = subprocess.Popen([sys.executable, str(SERVER)],
                            env={**os.environ, "MQTT_PUBLISH_PORT": str(port), **env},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 30
    while time.time() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return proc, f"http://127.0.0.1:{port}/mcp"
        time.sleep(0.2)
    proc.kill()
    raise RuntimeError("mqtt publisher did not start")


@pytest.fixture
def publisher(broker):
    proc, url = _start({"MQTT_PUBLISH_URL": f"mqtt://127.0.0.1:{broker}",
                        "MQTT_PUBLISH_ALLOW": "agent-hq/agents/#,homelab/cmd/+/power",
                        "MQTT_PUBLISH_MAX_BYTES": "200", "MQTT_PUBLISH_RATE_PER_MINUTE": "5"})
    yield url
    proc.terminate()
    proc.wait(10)


async def _call(server: str, tool: str, **args):
    async with Client(server) as c:
        res = await c.call_tool(tool, args)
        return res.structured_content if res.structured_content is not None else json.loads(res.content[0].text)


class Listener:
    """Subscribes to # on the broker and collects what arrives."""

    def __init__(self, port: int) -> None:
        self.port, self.got, self.ready = port, [], asyncio.Event()

    async def run(self) -> None:
        async with aiomqtt.Client("127.0.0.1", self.port, identifier="test-listener") as c:
            await c.subscribe("#", qos=1)
            self.ready.set()
            async for m in c.messages:
                self.got.append((str(m.topic), bytes(m.payload).decode(), m.retain))

    async def wait(self, n: int, timeout: float = 10) -> list:
        for _ in range(int(timeout * 20)):
            if len(self.got) >= n:
                return self.got
            await asyncio.sleep(0.05)
        raise AssertionError(f"expected {n} messages, got {self.got}")


@pytest.fixture
async def listener(broker):
    lst = Listener(broker)
    task = asyncio.create_task(lst.run())
    await asyncio.wait_for(lst.ready.wait(), 10)
    yield lst
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


# --------------------------------------------------------------------------- unit

@pytest.mark.parametrize("topic,ok", [
    ("homelab/cmd/pve1/power", True),
    ("homelab/cmd/pve1/reboot", False),     # not on the allow-list
    ("homelab/cmd/+/power", False),         # wildcard
    ("$SYS/broker/uptime", False),          # system topic
    ("agent-hq/notifications", False),      # denied by default
    ("", False),
])
def test_topic_checks(topic, ok):
    allow = ["homelab/cmd/+/power", "agent-hq/#"]
    if ok:
        assert pub.check_topic(topic, allow=allow) == topic
    else:
        with pytest.raises(pub.PublishError):
            pub.check_topic(topic, allow=allow)


def test_payload_encoding_and_names():
    assert pub.encode("WAN up") == b"WAN up"
    assert pub.encode({"a": 1}) == b'{"a":1}'
    assert pub.encode(None) == b""
    assert pub.slug("Snake Eyes") == "snake-eyes"
    with pytest.raises(pub.PublishError):
        pub.slug("  !! ")


# --------------------------------------------------------------------------- server over MCP

async def test_status_and_allow_listed_publishes_reach_the_broker(publisher, listener):
    info = await _call(publisher, "list_publish_topics")
    assert info["allowed"] == ["agent-hq/agents/#", "homelab/cmd/+/power"] and info["broker_configured"]

    r = await _call(publisher, "publish_status", name="Duke", state="ok", detail="Backups checked",
                    data={"warnings": 2})
    assert r == {"published": True, "topic": "agent-hq/agents/duke/status", "bytes": r["bytes"], "retained": True}
    r = await _call(publisher, "publish", topic="homelab/cmd/pve1/power", payload={"action": "on"})
    assert r["published"] and r["retained"] is False

    got = await listener.wait(2)
    topic, body, _ = got[0]
    status = json.loads(body)
    assert topic == "agent-hq/agents/duke/status"
    assert (status["agent"], status["state"], status["detail"], status["data"]) == (
        "Duke", "ok", "Backups checked", {"warnings": 2})
    assert got[1][:2] == ("homelab/cmd/pve1/power", '{"action":"on"}')


async def test_refusals(publisher, listener):
    for topic in ("homelab/cmd/pve1/reboot", "homelab/#", "$SYS/x", "agent-hq/notifications"):
        r = await _call(publisher, "publish", topic=topic, payload="x")
        assert "error" in r, (topic, r)
    r = await _call(publisher, "publish", topic="agent-hq/agents/duke/log", payload="x" * 500)
    assert "limit is 200" in r["error"]
    r = await _call(publisher, "publish", topic="agent-hq/agents/duke/log", payload="x", qos=3)
    assert "qos" in r["error"]
    # Rate limit: 5 per minute (the qos=3 call above was refused before taking a slot).
    results = [await _call(publisher, "publish", topic="agent-hq/agents/duke/log", payload=str(i))
               for i in range(6)]
    assert all(x.get("published") for x in results[:5]) and "rate limit" in results[5]["error"]
    await listener.wait(5)
    assert [t for t, _, _ in listener.got] == ["agent-hq/agents/duke/log"] * 5


async def test_no_broker_configured_is_explained():
    proc, url = _start({"MQTT_PUBLISH_URL": ""})
    try:
        r = await _call(url, "publish_status", name="Duke", state="ok")
        assert "MQTT_PUBLISH_URL" in r["error"]
    finally:
        proc.terminate()
        proc.wait(10)


# --------------------------------------------------------------------------- through Agent HQ

def announcer(messages, info):
    returns = _tool_returns(messages)
    if not returns:
        return ModelResponse(parts=[ToolCallPart("publish", {"topic": "homelab/cmd/nas/power",
                                                             "payload": {"action": "off"}})])
    return ModelResponse(parts=[TextPart(f"Publish result: {json.dumps(returns[-1].content, sort_keys=True)}")])


async def test_an_agent_publishes_after_approval(hq, publisher, listener):
    SCRIPTS["announcer"] = announcer
    srv = (await hq.http.post("/api/tool-servers", json={
        "name": "mqtt", "transport": "http", "url": publisher, "prefix_tools": False})).json()
    tools = {t["name"]: t for t in (await hq.http.get(f"/api/tool-servers/{srv['id']}/tools")).json()}
    assert tools["publish"]["approval"] == "ask"
    assert tools["publish_status"]["approval"] == "allow" and tools["list_publish_topics"]["approval"] == "allow"

    await hq.start_worker()
    a = await hq.agent(name="Duke", model="fake:announcer", tool_server_ids=[srv["id"]], memory_enabled=False)
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "Power off the NAS")
    for _ in range(400):
        rows = (await hq.http.get("/api/tool-approvals?status=pending")).json()
        if rows:
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("no approval request appeared")
    assert rows[0]["tool"] == "publish" and listener.got == []  # nothing sent before you approve
    await hq.http.post(f"/api/tool-approvals/{rows[0]['id']}/decide", json={"approve": True})

    replies = await hq.wait_agent_replies(t["id"], 1, timeout=30)
    assert '"published": true' in replies[0] and '"topic": "homelab/cmd/nas/power"' in replies[0]
    assert (await listener.wait(1))[0][:2] == ("homelab/cmd/nas/power", '{"action":"off"}')
