"""Vision tools: the agent's model asks questions and clicks by description; a separate
vision model looks at real (rendered) screenshots. The fake vision model genuinely finds
the red button's pixels in whatever image it is given, so zoom crops and coordinate
conversion are exercised for real."""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

import pytest
from PIL import Image
from pydantic_ai import BinaryContent, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo

from app.vision import crop_zoom, parse_point

from .conftest import SCRIPTS, _last_prompt, _tool_returns

FAKE_DESKTOP = Path(__file__).parent / "fake_desktop_mcp.py"
SEEN = {"main_tools": set(), "vision_images": []}


def _image_and_prompt(messages):
    for m in reversed(messages):
        for p in getattr(m, "parts", []):
            if p.part_kind == "user-prompt" and isinstance(p.content, list):
                text = next(c for c in p.content if isinstance(c, str))
                img = next(c for c in p.content if isinstance(c, BinaryContent))
                return img.data, text
    raise AssertionError("vision model got no image")


def red_center(png: bytes):
    im = Image.open(io.BytesIO(png)).convert("RGB")
    w, h = im.size
    xs, ys = [], []
    px = im.load()
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            r, g, b = px[x, y]
            if r > 180 and g < 90 and b < 90:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None, (w, h)
    return ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2), (w, h)


def fake_vision(messages, info: AgentInfo) -> ModelResponse:
    png, prompt = _image_and_prompt(messages)
    SEEN["vision_images"].append(Image.open(io.BytesIO(png)).size)
    if "Question:" in prompt:
        return ModelResponse(parts=[TextPart("A red 'Sign in' button near the bottom right.")])
    center, (w, h) = red_center(png)
    if center is None or "unicorn" in prompt:
        return ModelResponse(parts=[TextPart('{"found": false}')])
    if "pixels" in prompt:
        return ModelResponse(parts=[TextPart(json.dumps({"x": center[0], "y": center[1]}))])
    return ModelResponse(parts=[TextPart(json.dumps({"x": round(center[0] * 1000 / w),
                                                     "y": round(center[1] * 1000 / h)}))])


def vision_user(messages, info: AgentInfo) -> ModelResponse:
    SEEN["main_tools"] |= {t.name for t in info.function_tools}
    prompt = _last_prompt(messages)
    returns = _tool_returns(messages)
    if not returns:
        if prompt.startswith("look"):
            return ModelResponse(parts=[ToolCallPart("look", {"question": "What is on screen?"})])
        target = "a unicorn" if "unicorn" in prompt else "the red Sign in button"
        return ModelResponse(parts=[ToolCallPart("click_on", {"description": target})])
    return ModelResponse(parts=[TextPart(f"result: {json.dumps(returns[0].content) if not isinstance(returns[0].content, str) else returns[0].content}")])


SCRIPTS.update({"vision": fake_vision, "vision-user": vision_user})


async def _setup(hq, tmp_path, monkeypatch, coords="relative_1000", zoom=True):
    log = tmp_path / "clicks.txt"
    monkeypatch.setenv("CLICK_LOG", str(log))
    srv = (await hq.http.post("/api/tool-servers", json={
        "name": "desk", "transport": "stdio", "command": sys.executable,
        "args": [str(FAKE_DESKTOP)], "env": {"CLICK_LOG": str(log)}, "desktop": True})).json()
    a = await hq.agent(name="Eyes", model="fake:vision-user", tool_server_ids=[srv["id"]],
                       vision_model="fake:vision", vision_coords=coords, vision_zoom=zoom)
    t = await hq.thread(a["id"])
    SEEN["main_tools"].clear()
    SEEN["vision_images"].clear()
    return a, t, log


def _clicks(log: Path):
    return [tuple(map(float, line.split(",")[:2])) for line in log.read_text().splitlines()]


@pytest.mark.parametrize("coords,zoom", [("relative_1000", True), ("relative_1000", False), ("pixels", True)])
async def test_click_on_by_description(hq, tmp_path, monkeypatch, coords, zoom):
    await hq.start_worker()
    _, t, log = await _setup(hq, tmp_path, monkeypatch, coords, zoom)
    await hq.say(t["id"], "sign in please")
    [reply] = await hq.wait_agent_replies(t["id"], 1)
    assert '"clicked": true' in reply
    [(x, y)] = _clicks(log)
    assert 900 <= x <= 1000 and 600 <= y <= 640  # inside the button
    assert abs(x - 950) <= 4 and abs(y - 620) <= 4
    # The main model never had a screenshot tool; the vision model saw the images.
    assert not any(n.endswith("screenshot") for n in SEEN["main_tools"])
    assert {"look", "locate", "click_on"} <= SEEN["main_tools"]
    assert SEEN["vision_images"][0] == (1280, 720)
    if zoom:
        assert len(SEEN["vision_images"]) == 2 and SEEN["vision_images"][1] != (1280, 720)
    else:
        assert len(SEEN["vision_images"]) == 1


async def test_look_answers_in_text(hq, tmp_path, monkeypatch):
    await hq.start_worker()
    _, t, _ = await _setup(hq, tmp_path, monkeypatch)
    await hq.say(t["id"], "look at the screen")
    [reply] = await hq.wait_agent_replies(t["id"], 1)
    assert reply == "result: A red 'Sign in' button near the bottom right."


async def test_not_found_is_reported_not_clicked(hq, tmp_path, monkeypatch):
    await hq.start_worker()
    _, t, log = await _setup(hq, tmp_path, monkeypatch)
    await hq.say(t["id"], "click the unicorn")
    [reply] = await hq.wait_agent_replies(t["id"], 1)
    assert '"clicked": false' in reply and not log.exists()


async def test_vision_respects_desktop_handoff(hq, tmp_path, monkeypatch):
    await hq.start_worker()
    a, t, log = await _setup(hq, tmp_path, monkeypatch)
    await hq.http.post(f"/api/agents/{a['id']}/desktop/take", json={"thread_id": t["id"]})
    await hq.say(t["id"], "look now")
    [reply] = await hq.wait_agent_replies(t["id"], 1)
    assert "The user has taken control" in reply and not SEEN["vision_images"]


@pytest.mark.parametrize("text,mode,expect", [
    ('{"x": 500, "y": 250}', "relative_1000", (640, 180)),
    ('{"point_2d": [500, 250]}', "relative_1000", (640, 180)),
    ('{"bbox_2d": [400, 200, 600, 300]}', "relative_1000", (640, 180)),
    ("click(640, 180)", "pixels", (640, 180)),
    ('{"found": false}', "relative_1000", None),
    ("The element is not visible.", "pixels", None),
    ('{"x": 5000, "y": 10}', "pixels", None),
])
def test_parse_point(text, mode, expect):
    got = parse_point(text, mode, 1280, 720)
    assert (None if got is None else tuple(round(v) for v in got)) == expect


def test_crop_zoom_maps_back():
    im = Image.new("RGB", (1280, 720), "white")
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    crop, (left, top, scale) = crop_zoom(buf.getvalue(), 1250, 700)
    w, h = Image.open(io.BytesIO(crop)).size
    assert left + w / scale <= 1280.5 and top + h / scale <= 720.5  # stays on screen
    assert scale > 1  # enlarged for the second look


async def test_desktop_agents_know_whether_they_can_see_images(hq, tmp_path, monkeypatch):
    # Regression (trace 2026-11-08): with no vision model, Snake Eyes searched the page text
    # for "gun", found nothing and reported that, when the answer was in the photo.
    from .conftest import _instructions

    seen = []
    SCRIPTS["rules"] = lambda messages, info: (seen.append(_instructions(messages)),
                                               ModelResponse(parts=[TextPart("ok")]))[1]
    await hq.start_worker()
    srv = (await hq.http.post("/api/tool-servers", json={
        "name": "desk", "transport": "stdio", "command": sys.executable,
        "args": [str(FAKE_DESKTOP)], "desktop": True})).json()
    blind = await hq.agent(name="Blind", model="fake:rules", tool_server_ids=[srv["id"]])
    sighted = await hq.agent(name="Sighted", model="fake:rules", tool_server_ids=[srv["id"]],
                             vision_model="fake:vision")
    for a in (blind, sighted):
        t = await hq.thread(a["id"])
        await hq.say(t["id"], "what does the gun in the photo look like?")
        await hq.wait_agent_replies(t["id"], 1)
    assert "no vision model is set for you" in seen[0] and 'report status "partial"' in seen[0]
    assert "photos or illustrations" not in seen[0]
    assert "photos or illustrations, especially when" in seen[1] and "no vision model" not in seen[1]


async def test_a_provider_without_a_model_is_refused(hq):
    p = (await hq.http.post("/api/providers", json={"name": "ChatGPT Plus", "kind": "chatgpt"})).json()
    r = await hq.http.post("/api/agents", json={"name": "Duke", "provider_id": p["id"], "model": ""})
    assert r.status_code == 422 and "choose a model" in r.json()["detail"]
    r = await hq.http.post("/api/agents", json={"name": "Duke", "provider_id": p["id"], "model": "gpt-6.1",
                                                "fallback_provider_id": p["id"]})
    assert r.status_code == 422 and "fallback model" in r.json()["detail"]
