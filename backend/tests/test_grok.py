"""Grok plan providers (Sign in with xAI): the device sign-in from the UI, credentials stored
server-side and never returned, model listing, an agent turn on the subscription through
Chat Completions with thinking sent as reasoning_effort, and refreshes serialized across
workers.

xAI is faked with an httpx2 MockTransport that enforces the parts of the contract that
matter: the device grant (pending until approved, slow_down, denial), the client ID,
single-use rotating refresh tokens, and bearer tokens.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from urllib.parse import parse_qs

import httpx2
import pytest

from app import db, grok
from app.models import ModelProvider


def _jwt(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


class FakeXai:
    def __init__(self) -> None:
        self.devices: dict[str, str] = {}  # device_code -> pending | approved | denied
        self.polls = 0
        self.slow_down_once = False
        self.refresh_token = ""
        self.access_token = ""
        self.refreshes = 0
        self.reused = 0
        self.access_ttl = 21600
        self.chats: list[dict] = []
        self.entitled = True

    def approve(self, user_code: str = "") -> None:
        for k, v in self.devices.items():
            if v == "pending":
                self.devices[k] = "approved"

    def _tokens(self) -> dict:
        self.access_token = f"xat-{time.time_ns()}"
        self.refresh_token = f"xrt-{time.time_ns()}"
        return {"access_token": self.access_token, "refresh_token": self.refresh_token,
                "token_type": "Bearer", "expires_in": self.access_ttl,
                "scope": grok.SCOPE,
                "id_token": _jwt({"iss": "https://auth.x.ai", "email": "mike@example.com"})}

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        if url.startswith("https://auth.x.ai/"):
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            assert form["client_id"] == grok.GROK_CLI_CLIENT_ID
            if url == grok.DEVICE_CODE_URL:
                assert form["scope"] == grok.SCOPE
                code = f"dev-{len(self.devices)}"
                self.devices[code] = "pending"
                return httpx2.Response(200, json={
                    "device_code": code, "user_code": "ABCD-1234",
                    "verification_uri": "https://accounts.x.ai/device",
                    "verification_uri_complete": "https://accounts.x.ai/device?user_code=ABCD-1234",
                    "expires_in": 600, "interval": 5})
            assert url == grok.TOKEN_URL
            if form["grant_type"] == grok.DEVICE_GRANT:
                self.polls += 1
                if self.slow_down_once:
                    self.slow_down_once = False
                    return httpx2.Response(400, json={"error": "slow_down"})
                st = self.devices.get(form["device_code"])
                if st == "pending":
                    return httpx2.Response(400, json={"error": "authorization_pending"})
                if st == "denied":
                    return httpx2.Response(400, json={"error": "access_denied"})
                if st != "approved":
                    return httpx2.Response(400, json={"error": "invalid_grant"})
                del self.devices[form["device_code"]]  # one use
                if not self.entitled:
                    return httpx2.Response(403, json={"error": "forbidden"})
                return httpx2.Response(200, json=self._tokens())
            assert form["grant_type"] == "refresh_token"
            await asyncio.sleep(0.3)  # slow enough that unserialized refreshes would collide
            if form["refresh_token"] != self.refresh_token:
                self.reused += 1
                return httpx2.Response(400, json={"error": "invalid_grant"})
            self.refreshes += 1
            body = self._tokens()
            del body["id_token"]
            return httpx2.Response(200, json=body)
        assert url.startswith("https://api.x.ai/v1/"), url
        if request.headers.get("authorization") != f"Bearer {self.access_token}":
            return httpx2.Response(401, json={"error": "bad token"})
        if url == "https://api.x.ai/v1/models":
            return httpx2.Response(200, json={"object": "list", "data": [
                {"id": "grok-4.6", "object": "model", "owned_by": "xai"},
                {"id": "grok-imagine-image", "object": "model", "owned_by": "xai"},
                {"id": "grok-4.3", "object": "model", "owned_by": "xai"}]})
        if url == "https://api.x.ai/v1/chat/completions":
            body = json.loads(request.content)
            self.chats.append(body)
            msg = {"role": "assistant", "content": "Hello from your Grok plan."}
            usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
            if body.get("stream"):
                chunks = [{"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": body["model"],
                           "choices": [{"index": 0, "delta": msg, "finish_reason": None}]},
                          {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": body["model"],
                           "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage}]
                data = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
                return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=data.encode())
            return httpx2.Response(200, json={"id": "c1", "object": "chat.completion", "created": 0,
                                              "model": body["model"], "usage": usage,
                                              "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}]})
        return httpx2.Response(404)


@pytest.fixture
def xai_fake(monkeypatch):
    fake = FakeXai()
    monkeypatch.setattr(grok, "_transport", httpx2.MockTransport(fake.handle))
    return fake


async def _poll_until_done(hq, pid: str) -> dict:
    # The server refuses to ask xAI faster than the interval, so wind its clock on.
    for _ in range(5):
        r = await hq.http.post(f"/api/providers/{pid}/grok/poll")
        assert r.status_code == 200, r.text
        if r.json()["state"] == "done":
            return r.json()["provider"]
        key = grok._PENDING + pid
        pending = json.loads(await hq.broker.r.get(key))
        pending["next_poll"] = 0
        await hq.broker.r.set(key, json.dumps(pending), ex=600)
    raise AssertionError("sign-in never finished")


async def _signed_in(hq, fake) -> dict:
    p = (await hq.http.post("/api/providers", json={"name": "SuperGrok", "kind": "grok"})).json()
    await hq.http.post(f"/api/providers/{p['id']}/grok/start")
    fake.approve()
    return await _poll_until_done(hq, p["id"])


async def test_device_sign_in_from_the_ui(hq, xai_fake):
    p = (await hq.http.post("/api/providers", json={
        "name": "SuperGrok", "kind": "grok", "base_url": "http://ignored", "api_key": "x"})).json()
    assert p["base_url"] is None and p["has_api_key"] is False
    assert p["grok"] == {"connected": False, "account": None, "connected_at": None}

    start = (await hq.http.post(f"/api/providers/{p['id']}/grok/start")).json()
    assert start["user_code"] == "ABCD-1234" and start["interval"] == 5
    assert start["verification_uri_complete"].startswith("https://accounts.x.ai/device")
    assert "device_code" not in json.dumps(start)  # stays on the server

    # Not approved yet: pending. Asking again straight away doesn't bother xAI.
    r = (await hq.http.post(f"/api/providers/{p['id']}/grok/poll")).json()
    assert r == {"state": "pending", "interval": 5} and xai_fake.polls == 1
    await hq.http.post(f"/api/providers/{p['id']}/grok/poll")
    assert xai_fake.polls == 1

    xai_fake.approve()
    done = await _poll_until_done(hq, p["id"])
    assert done["grok"]["connected"] and done["grok"]["account"] == "mike@example.com"
    assert "oauth" not in json.dumps(done) and xai_fake.refresh_token not in json.dumps(done)
    # Finished: the pending sign-in is gone.
    assert (await hq.http.post(f"/api/providers/{p['id']}/grok/poll")).status_code == 422

    export = (await hq.http.get("/api/agents/export")).json()
    assert "refresh_token" not in json.dumps(export)

    d = (await hq.http.post(f"/api/providers/{p['id']}/grok/disconnect")).json()
    assert d["grok"]["connected"] is False
    with db.session() as s:
        assert s.get(ModelProvider, p["id"]).oauth is None


async def test_slow_down_declined_and_not_entitled(hq, xai_fake):
    p = (await hq.http.post("/api/providers", json={"name": "Grok", "kind": "grok"})).json()
    await hq.http.post(f"/api/providers/{p['id']}/grok/start")
    xai_fake.slow_down_once = True
    r = (await hq.http.post(f"/api/providers/{p['id']}/grok/poll")).json()
    assert r == {"state": "pending", "interval": 10}

    for k in xai_fake.devices:
        xai_fake.devices[k] = "denied"
    key = grok._PENDING + p["id"]
    pending = json.loads(await hq.broker.r.get(key))
    pending["next_poll"] = 0
    await hq.broker.r.set(key, json.dumps(pending), ex=600)
    r = await hq.http.post(f"/api/providers/{p['id']}/grok/poll")
    assert r.status_code == 422 and "declined" in r.json()["detail"]

    xai_fake.entitled = False
    await hq.http.post(f"/api/providers/{p['id']}/grok/start")
    xai_fake.approve()
    r = await hq.http.post(f"/api/providers/{p['id']}/grok/poll")
    assert r.status_code == 422 and "SuperGrok" in r.json()["detail"]


async def test_models_and_an_agent_turn_on_the_plan(hq, xai_fake):
    p = await _signed_in(hq, xai_fake)
    models = (await hq.http.get(f"/api/providers/{p['id']}/models")).json()
    assert [m["id"] for m in models] == ["grok-4.3", "grok-4.6"]  # image model left out

    await hq.start_worker()
    a = await hq.agent(name="Duke", instructions="You lead the team.", provider_id=p["id"],
                       model="grok-4.3", memory_enabled=False, thinking="high")
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "status?")
    assert await hq.wait_agent_replies(t["id"], 1) == ["Hello from your Grok plan."]
    body = next(c for c in xai_fake.chats if "You lead the team." in json.dumps(c))
    assert body["model"] == "grok-4.3" and body["reasoning_effort"] == "high"


async def test_thinking_maps_to_what_each_grok_model_accepts():
    from pydantic_ai.models.openai import OpenAIChatModel  # noqa: F401  (profile only)

    p43, p46, old = grok.model_profile("grok-4.3"), grok.model_profile("grok-4.6"), grok.model_profile("grok-2")
    assert p43["supports_thinking"] and "none" in p43["openai_reasoning_efforts"]
    assert p46["thinking_always_enabled"] and "none" not in p46["openai_reasoning_efforts"]
    assert not old["supports_thinking"] and not old["openai_supports_reasoning"]


async def test_a_not_signed_in_provider_says_what_to_do(hq, xai_fake):
    p = (await hq.http.post("/api/providers", json={"name": "Grok", "kind": "grok"})).json()
    r = await hq.http.get(f"/api/providers/{p['id']}/models")
    assert r.status_code == 502 and "Sign in with xAI" in r.json()["detail"]


async def test_switching_kind_drops_the_sign_in(hq, xai_fake):
    p = await _signed_in(hq, xai_fake)
    r = (await hq.http.put(f"/api/providers/{p['id']}", json={"name": "Grok", "kind": "chatgpt"})).json()
    assert r["chatgpt"]["connected"] is False
    with db.session() as s:
        assert s.get(ModelProvider, p["id"]).oauth is None


async def test_workers_never_spend_the_same_refresh_token(hq, xai_fake):
    p = await _signed_in(hq, xai_fake)
    first_refresh = xai_fake.refresh_token
    with db.session() as s:
        r = s.get(ModelProvider, p["id"])
        o = dict(r.oauth)
        o["expires_at"] = time.time() + 60  # inside the refresh window
        r.oauth = o
        s.add(r)
        s.commit()
        s.refresh(r)
        row = r
    a, b = grok.make_provider(row), grok.make_provider(row)
    out = await asyncio.gather(a.client.get("/models", cast_to=object), b.client.get("/models", cast_to=object))
    assert all(len(body["data"]) == 3 for body in out)
    assert xai_fake.refreshes == 1 and xai_fake.reused == 0
    with db.session() as s:
        stored = s.get(ModelProvider, p["id"]).oauth
    assert stored["refresh_token"] == xai_fake.refresh_token != first_refresh
    assert stored["id_token"]  # kept from sign-in when the refresh carries none
    assert stored["expires_at"] > time.time() + 3600
