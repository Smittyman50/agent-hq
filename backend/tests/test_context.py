"""Long conversations: context windows, compaction between turns, trimming within a turn,
and the warning when instructions alone don't fit."""

from __future__ import annotations

import functools
import http.server
import json
import threading

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart

from app import context
from app.runtime import load_agent


def _turn(i: int, big: int = 400, tool: bool = False):
    msgs = [ModelRequest(parts=[UserPromptPart(content=f"question {i}: " + "x" * big)])]
    if tool:
        msgs += [ModelResponse(parts=[ToolCallPart("lookup", {"q": i}, tool_call_id=f"c{i}")]),
                 ModelRequest(parts=[ToolReturnPart("lookup", "r" * big, tool_call_id=f"c{i}")])]
    msgs.append(ModelResponse(parts=[TextPart(content=f"answer {i}")]))
    return msgs


def _history(n: int, **kw):
    return [m for i in range(n) for m in _turn(i, tool=i % 2 == 0, **kw)]


def test_cuts_only_at_turn_starts():
    h = _history(6)
    starts = context.turn_starts(h)
    assert len(starts) == 6 and all(isinstance(h[i].parts[0], UserPromptPart) for i in starts)
    cut = context._plan(h, budget=400, keep_ratio=0.5)
    assert cut == starts[-1]  # only the latest turn fits its share
    assert context._plan(_history(1), 10, 0.5) is None  # nothing older to summarise


async def test_compaction_summarises_older_turns(hq):
    await hq.http.put("/api/conversations/settings", json={"model": "fake:summarizer"})
    a = await hq.agent(name="Duke", model="fake:echo")
    agent = load_agent(a["id"])
    h = _history(8)
    out, done = await context.compact(agent, h, budget=600, window=4096)
    assert done.method == "summary" and done.turns >= 6 and done.after < done.before
    assert context.is_summary(out) and isinstance(out[1], ModelResponse)
    assert out[-1].parts[0].content == "answer 7"  # the latest turn is kept word for word
    assert "earlier user turns; first asked: question 0" in out[0].parts[0].content
    # A second compaction folds the old summary into the new one.
    more = out + _history(6)
    again, done2 = await context.compact(agent, more, budget=600, window=4096)
    assert context.is_summary(again) and done2 is not None
    assert sum(context.is_summary([m]) for m in again) == 1
    # Under budget: nothing happens.
    same, none = await context.compact(agent, _history(2), budget=100_000, window=4096)
    assert none is None and len(same) == len(_history(2))


async def test_compaction_falls_back_to_dropping_when_the_summary_fails(hq):
    await hq.http.put("/api/conversations/settings", json={"model": "fake:broken"})
    a = await hq.agent(name="Duke", model="fake:echo")
    out, done = await context.compact(load_agent(a["id"]), _history(6), budget=500, window=4096)
    assert done.method == "dropped" and out[0].parts[0].content == context.DROPPED_NOTE


async def test_long_conversations_get_compacted_in_place(hq):
    await hq.http.put("/api/conversations/settings", json={"model": "fake:summarizer", "reserve_tokens": 100})
    await hq.start_worker()
    a = await hq.agent(name="Duke", model="fake:historian", context_window=2000, memory_enabled=False)
    t = await hq.thread(a["id"])
    for i in range(6):
        await hq.say(t["id"], f"message {i}: " + "words " * 150)
        await hq.wait_agent_replies(t["id"], i + 1)
    replies = await hq.wait_agent_replies(t["id"], 6)
    events = [m for m in hq.messages(t["id"]) if m.meta.get("kind") == "compaction"]
    assert events and events[0].meta["method"] == "summary" and "2,000-token context window" in events[0].content
    assert "earlier user turns" in events[0].meta["summary"]
    assert replies[-1].endswith("summary=True")  # the model was given the summary, not the full history


async def test_warns_once_when_instructions_dont_fit(hq):
    await hq.start_worker()
    a = await hq.agent(name="Duke", model="fake:echo", context_window=1024, instructions="Be thorough. " * 400)
    t = await hq.thread(a["id"])
    for i in range(2):
        await hq.say(t["id"], f"hi {i}")
        await hq.wait_agent_replies(t["id"], i + 1)
    warnings = [m for m in hq.messages(t["id"]) if m.meta.get("kind") == "context_warning"]
    assert len(warnings) == 1 and "1,024" in warnings[0].content and "OLLAMA_CONTEXT_LENGTH" in warnings[0].content
    info = (await hq.http.get(f"/api/agents/{a['id']}/context")).json()
    assert info["window"] == 1024 and info["source"] == "set on the agent" and info["instruction_tokens"] > 1024
    assert info["room_for_conversation"] == 0


def test_trimming_within_a_turn():
    msgs = [ModelRequest(parts=[UserPromptPart(content="go")])]
    for i in range(6):
        msgs += [ModelResponse(parts=[ToolCallPart("page", {"n": i}, tool_call_id=f"t{i}")]),
                 ModelRequest(parts=[ToolReturnPart("page", "p" * 3000, tool_call_id=f"t{i}")])]
    trimmed = context.make_trimmer(1500)(msgs)
    sizes = [len(p.content) for m in trimmed for p in m.parts if isinstance(p, ToolReturnPart)]
    assert sizes[-1] == 3000 and max(sizes[:-1]) < 300  # the latest result is whole
    assert context.make_trimmer(0)(msgs)[2].parts[0].content == "p" * 3000  # no window, no change


def test_a_result_bigger_than_the_window_is_cut_to_fit():
    # A full snapshot of a long web page: sent whole, the model server would drop the start
    # of the prompt (the user's request) instead.
    msgs = [ModelRequest(parts=[UserPromptPart(content="describe the photo on this page")]),
            ModelResponse(parts=[ToolCallPart("browser_snapshot", {}, tool_call_id="s")]),
            ModelRequest(parts=[ToolReturnPart("browser_snapshot", "x" * 400_000, tool_call_id="s")])]
    trimmed = context.make_trimmer(8000)(msgs)
    assert context.history_tokens(trimmed) <= 8000
    assert trimmed[0].parts[0].content == "describe the photo on this page"
    cut = trimmed[2].parts[0].content
    assert cut.startswith("xxx") and "browser_snapshot result was about" in cut


@pytest.fixture
def fake_ollama():
    class H(http.server.BaseHTTPRequestHandler):
        def _send(self, body):
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/api/ps":
                self._send({"models": [{"name": "qwen3:14b", "model": "qwen3:14b", "context_length": 32768}]})
            else:
                self._send({"models": []})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            params = "num_ctx 16384\ntemperature 0.6" if body.get("model") == "gpt-oss:20b" else "temperature 0.7"
            self._send({"parameters": params})

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


async def test_window_comes_from_ollama(hq, fake_ollama):
    context._cache.clear()
    p = (await hq.http.post("/api/providers", json={"name": "Einstein", "kind": "ollama", "base_url": fake_ollama})).json()
    loaded = load_agent((await hq.agent(name="A", provider_id=p["id"], model="qwen3:14b"))["id"])
    modelfile = load_agent((await hq.agent(name="B", provider_id=p["id"], model="gpt-oss:20b"))["id"])
    unknown = load_agent((await hq.agent(name="C", provider_id=p["id"], model="llama3.2:3b"))["id"])
    assert await context.window_for(loaded) == (32768, "Ollama (the loaded model)")
    assert await context.window_for(modelfile) == (16384, "Ollama (the model's num_ctx)")
    w, src = await context.window_for(unknown)
    assert w == 4096 and src.startswith("a guess")
    set_on = load_agent((await hq.agent(name="D", provider_id=p["id"], model="llama3.2:3b", context_window=65536))["id"])
    assert await context.window_for(set_on) == (65536, "set on the agent")


async def test_settings_are_clamped(hq):
    r = (await hq.http.put("/api/conversations/settings", json={"trigger_ratio": 2, "local_window": 10})).json()
    assert r["trigger_ratio"] == 0.95 and r["local_window"] == 1024
