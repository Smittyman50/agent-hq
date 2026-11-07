"""ChatGPT plan providers (Sign in with ChatGPT): sign-in from the UI by pasting the callback
address, credentials stored server-side and never returned, model listing, an agent turn
running on the plan through the Responses API, and refreshes serialized across workers.

OpenAI is faked with an httpx2 MockTransport that enforces the parts of the contract that
matter: PKCE, the issued client ID, single-use rotating refresh tokens, bearer tokens,
and store=false / stream=true with no unsupported fields on inference requests.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from pydantic_ai.providers import openai_chatgpt as pa_chatgpt

from app import chatgpt, db
from app.models import ModelProvider

ISSUED = "oaiapp_test123"


def _jwt(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


class FakeOpenAI:
    def __init__(self) -> None:
        self.challenges: dict[str, dict] = {}  # code -> {challenge, nonce, client_id}
        self.refresh_token = ""
        self.access_token = ""
        self.refreshes = 0
        self.reused = 0  # real OpenAI may revoke the whole sign-in when this happens
        self.responses: list[dict] = []
        self.access_ttl = 3600
        self.grant_plan = True

    # what the authorize page would do after you approve: issue a code for this request
    def approve(self, authorize_url: str) -> str:
        q = {k: v[0] for k, v in parse_qs(urlsplit(authorize_url).query).items()}
        code = f"code-{len(self.challenges)}"
        self.challenges[code] = {"challenge": q["code_challenge"], "nonce": q["nonce"]}
        return (f"{q['redirect_uri']}?code={code}&state={q['state']}&scope=x"
                + (f"&client_id={ISSUED}" if q["client_id"] == "dynamic_agent_client" else ""))

    def _tokens(self, nonce: str | None = None) -> dict:
        self.access_token = _jwt({"exp": int(time.time()) + self.access_ttl, "n": time.time_ns()})
        self.refresh_token = f"rt-{time.time_ns()}"
        scope = "openid profile email offline_access resource.invoke"
        if self.grant_plan:
            scope += " chatgpt.tokens.use.direct"
        out = {"access_token": self.access_token, "refresh_token": self.refresh_token,
               "token_type": "Bearer", "expires_in": self.access_ttl, "scope": scope}
        out["id_token"] = _jwt({"iss": "https://auth.openai.com", "aud": ISSUED, "sub": "user-1",
                                "email": "mike@example.com", "exp": int(time.time()) + 3600,
                                "nonce": nonce or ""})
        return out

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        if url == "https://auth.openai.com/api/accounts/oauth/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            assert form["resource"] == "https://api.openai.com/v1"
            assert form["client_id"] == ISSUED
            if form["grant_type"] == "authorization_code":
                c = self.challenges.pop(form["code"])
                digest = base64.urlsafe_b64encode(
                    hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=").decode()
                if digest != c["challenge"]:
                    return httpx2.Response(400, json={"error": "invalid_grant"})
                assert form["redirect_uri"] == "http://127.0.0.1:1455/auth/callback"
                return httpx2.Response(200, json=self._tokens(c["nonce"]))
            assert form["grant_type"] == "refresh_token"
            await asyncio.sleep(0.3)  # slow enough that unserialized refreshes would collide
            if form["refresh_token"] != self.refresh_token:
                self.reused += 1
                return httpx2.Response(400, json={"error": "refresh_token_reused"})
            self.refreshes += 1
            body = self._tokens()
            del body["id_token"]
            return httpx2.Response(200, json=body)
        if request.headers.get("authorization") != f"Bearer {self.access_token}":
            return httpx2.Response(401, json={"error": {"message": "bad token"}})
        if url == "https://api.openai.com/v1/models":
            return httpx2.Response(200, json={"object": "list", "data": [
                {"id": "gpt-6.1-sol", "object": "model", "created": 0, "owned_by": "openai",
                 "visibility": "list", "display_name": "GPT-6.1 Sol"},
                {"id": "internal-x", "object": "model", "created": 0, "owned_by": "openai",
                 "visibility": "hidden"},
                {"id": "gpt-6.1-mini", "object": "model", "created": 0, "owned_by": "openai",
                 "visibility": "list", "display_name": "GPT-6.1 mini"}]})
        if url == "https://api.openai.com/v1/responses":
            body = json.loads(request.content)
            self.responses.append(body)
            return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                                   content=_sse(body["model"], "Hello from your ChatGPT plan."))
        return httpx2.Response(404)


def _sse(model: str, text: str) -> bytes:
    resp = {"id": "resp_1", "object": "response", "created_at": 0, "model": model, "status": "in_progress",
            "output": [], "parallel_tool_calls": True, "tool_choice": "auto", "tools": []}
    msg = {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
           "content": [{"type": "output_text", "text": text, "annotations": []}]}
    done = {**resp, "status": "completed", "output": [msg],
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                      "input_tokens_details": {"cached_tokens": 0},
                      "output_tokens_details": {"reasoning_tokens": 0}}}
    events = [
        {"type": "response.created", "response": resp},
        {"type": "response.output_item.added", "output_index": 0,
         "item": {**msg, "status": "in_progress", "content": []}},
        {"type": "response.content_part.added", "item_id": "msg_1", "output_index": 0, "content_index": 0,
         "part": {"type": "output_text", "text": "", "annotations": []}},
        {"type": "response.output_text.delta", "item_id": "msg_1", "output_index": 0, "content_index": 0,
         "delta": text, "logprobs": []},
        {"type": "response.output_text.done", "item_id": "msg_1", "output_index": 0, "content_index": 0,
         "text": text, "logprobs": []},
        {"type": "response.output_item.done", "output_index": 0, "item": msg},
        {"type": "response.completed", "response": done},
    ]
    return "".join(f"event: {e['type']}\ndata: {json.dumps({**e, 'sequence_number': i})}\n\n"
                   for i, e in enumerate(events)).encode()


@pytest.fixture
def openai_fake(monkeypatch):
    fake = FakeOpenAI()
    transport = httpx2.MockTransport(fake.handle)
    monkeypatch.setattr(chatgpt, "_transport", transport)
    original = pa_chatgpt._post_token_request

    async def post(url, form, ta, **kw):  # the sign-in code exchange uses its own client
        if kw.get("http_client") is None:
            async with httpx2.AsyncClient(transport=transport) as c:
                kw["http_client"] = c
                return await original(url, form, ta, **kw)
        return await original(url, form, ta, **kw)

    monkeypatch.setattr(pa_chatgpt, "_post_token_request", post)
    return fake


async def _signed_in(hq, fake) -> dict:
    p = (await hq.http.post("/api/providers", json={"name": "ChatGPT Plus", "kind": "chatgpt"})).json()
    start = (await hq.http.post(f"/api/providers/{p['id']}/chatgpt/start")).json()
    r = await hq.http.post(f"/api/providers/{p['id']}/chatgpt/finish",
                           json={"callback_url": fake.approve(start["url"])})
    assert r.status_code == 200, r.text
    return r.json()


async def test_sign_in_by_pasting_the_callback_address(hq, openai_fake):
    p = (await hq.http.post("/api/providers", json={
        "name": "ChatGPT Plus", "kind": "chatgpt", "base_url": "http://ignored", "api_key": "x"})).json()
    assert p["base_url"] is None and p["has_api_key"] is False
    assert p["chatgpt"] == {"connected": False, "account": None, "plan_usage": False, "connected_at": None}

    start = (await hq.http.post(f"/api/providers/{p['id']}/chatgpt/start")).json()
    q = {k: v[0] for k, v in parse_qs(urlsplit(start["url"]).query).items()}
    assert start["url"].startswith("https://auth.openai.com/api/accounts/authorize?")
    assert q["client_id"] == "dynamic_agent_client" and q["agent_name_hint"] == "Agent HQ"
    assert q["redirect_uri"] == "http://127.0.0.1:1455/auth/callback"
    assert q["ext_agent_host_id"].startswith("urn:uuid:") and q["code_challenge_method"] == "S256"
    assert "chatgpt.tokens.use.direct" in q["scope"].split() and q["resource"] == "https://api.openai.com/v1"

    callback = openai_fake.approve(start["url"])
    # A tampered state, or garbage, is refused without spending the code.
    bad = await hq.http.post(f"/api/providers/{p['id']}/chatgpt/finish",
                             json={"callback_url": callback.replace("state=", "state=x")})
    assert bad.status_code == 422 and "expired" in bad.json()["detail"]
    assert (await hq.http.post(f"/api/providers/{p['id']}/chatgpt/finish",
                               json={"callback_url": "hello"})).status_code == 422

    done = (await hq.http.post(f"/api/providers/{p['id']}/chatgpt/finish",
                               json={"callback_url": callback})).json()
    assert done["chatgpt"]["connected"] and done["chatgpt"]["plan_usage"]
    assert done["chatgpt"]["account"] == "mike@example.com"
    assert "oauth" not in json.dumps(done) and openai_fake.refresh_token not in json.dumps(done)
    # The same callback can't be replayed.
    again = await hq.http.post(f"/api/providers/{p['id']}/chatgpt/finish", json={"callback_url": callback})
    assert again.status_code == 422

    # Signing in again reuses the registration (no dynamic registration, no name hint).
    start2 = (await hq.http.post(f"/api/providers/{p['id']}/chatgpt/start")).json()
    q2 = {k: v[0] for k, v in parse_qs(urlsplit(start2["url"]).query).items()}
    assert q2["client_id"] == ISSUED and "agent_name_hint" not in q2 and "id_token_hint" in q2
    assert q2["ext_agent_host_id"] == q["ext_agent_host_id"]

    # Exports never carry the sign-in; importing one back keeps it.
    export = (await hq.http.get("/api/agents/export")).json()
    assert "oauth" not in json.dumps(export) and "refresh_token" not in json.dumps(export)
    assert (await hq.http.post("/api/agents/import", json=export)).status_code == 200
    assert (await hq.http.get("/api/providers")).json()[0]["chatgpt"]["connected"] is True

    # Disconnect forgets the tokens but keeps the registration for next time.
    d = (await hq.http.post(f"/api/providers/{p['id']}/chatgpt/disconnect")).json()
    assert d["chatgpt"]["connected"] is False
    with db.session() as s:
        assert s.get(ModelProvider, p["id"]).oauth == {"client_id": ISSUED}


async def test_declined_or_no_plan_usage_is_explained(hq, openai_fake):
    p = (await hq.http.post("/api/providers", json={"name": "GPT", "kind": "chatgpt"})).json()
    start = (await hq.http.post(f"/api/providers/{p['id']}/chatgpt/start")).json()
    state = parse_qs(urlsplit(start["url"]).query)["state"][0]
    r = await hq.http.post(f"/api/providers/{p['id']}/chatgpt/finish", json={
        "callback_url": f"http://127.0.0.1:1455/auth/callback?error=access_denied&state={state}"})
    assert r.status_code == 422 and "declined" in r.json()["detail"]

    openai_fake.grant_plan = False
    start = (await hq.http.post(f"/api/providers/{p['id']}/chatgpt/start")).json()
    r = await hq.http.post(f"/api/providers/{p['id']}/chatgpt/finish",
                           json={"callback_url": openai_fake.approve(start["url"])})
    assert r.status_code == 422 and "plan" in r.json()["detail"]
    # Next attempt asks for consent again.
    start = (await hq.http.post(f"/api/providers/{p['id']}/chatgpt/start")).json()
    assert parse_qs(urlsplit(start["url"]).query)["prompt"] == ["consent"]


async def test_models_and_an_agent_turn_on_the_plan(hq, openai_fake):
    p = await _signed_in(hq, openai_fake)
    models = (await hq.http.get(f"/api/providers/{p['id']}/models")).json()
    assert models == [{"id": "gpt-6.1-sol", "details": "GPT-6.1 Sol"},
                      {"id": "gpt-6.1-mini", "details": "GPT-6.1 mini"}]  # OpenAI's order, hidden dropped

    await hq.start_worker()
    a = await hq.agent(name="Duke", instructions="You lead the team.", provider_id=p["id"],
                       model="gpt-6.1-sol", memory_enabled=False)
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "status?")
    replies = await hq.wait_agent_replies(t["id"], 1)
    assert replies == ["Hello from your ChatGPT plan."]
    body = openai_fake.responses[-1]
    assert body["store"] is False and body["stream"] is True and body["model"] == "gpt-6.1-sol"
    assert "You lead the team." in json.dumps(body)
    for unsupported in ("temperature", "max_output_tokens", "top_p", "previous_response_id"):
        assert unsupported not in body
    assert not any(isinstance(i, dict) and i.get("role") == "system" for i in body["input"])


async def test_a_not_signed_in_provider_says_what_to_do(hq, openai_fake):
    p = (await hq.http.post("/api/providers", json={"name": "GPT", "kind": "chatgpt"})).json()
    r = await hq.http.get(f"/api/providers/{p['id']}/models")
    assert r.status_code == 502 and "Sign in with ChatGPT" in r.json()["detail"]


async def test_workers_never_spend_the_same_refresh_token(hq, openai_fake):
    p = await _signed_in(hq, openai_fake)
    openai_fake.access_ttl = 1  # every token is already inside the refresh window
    with db.session() as s:
        row = s.get(ModelProvider, p["id"])
    first_refresh = openai_fake.refresh_token
    # Two "workers", each with its own provider instance, refresh at the same moment.
    openai_fake.access_token = _jwt({"exp": int(time.time()) - 10})
    o = dict(row.oauth)
    o["access_token"] = openai_fake.access_token
    with db.session() as s:
        r = s.get(ModelProvider, p["id"])
        r.oauth = o
        s.add(r)
        s.commit()
        s.refresh(r)
        row = r
    openai_fake.access_ttl = 3600
    a, b = chatgpt.make_provider(row), chatgpt.make_provider(row)
    out = await asyncio.gather(a.client.models.list(), b.client.models.list())
    assert all(len(page.data) == 3 for page in out)
    assert openai_fake.refreshes == 1  # one refresh; the other worker adopted the stored result
    assert openai_fake.reused == 0  # and nobody ever sent a spent refresh token
    with db.session() as s:
        stored = s.get(ModelProvider, p["id"]).oauth
    assert stored["refresh_token"] == openai_fake.refresh_token != first_refresh
    assert stored["id_token"]  # kept from sign-in when the refresh carries none
