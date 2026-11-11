"""Chat attachments: upload, the manifest in the turn, reading and looking at files,
images straight to a multimodal model (and out of the saved history), passing files to a
delegate, the workbench in and out, files agents send back, and the download headers."""

from __future__ import annotations

import io
import re
import sys
from pathlib import Path

import pytest
from PIL import Image
from pydantic_ai import BinaryContent, ModelResponse, TextPart, ToolCallPart

from app.config import settings
from app.models import Role

from .conftest import SCRIPTS, _last_prompt, _tool_returns

WB_DIR = Path(__file__).resolve().parents[2] / "workbench"
IDS = re.compile(r"att_[0-9a-f]{12}")
SEEN: dict[str, list] = {}


def png(color=(220, 20, 20), size=(64, 48)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _returns(messages, name):
    return [r for r in reversed(_tool_returns(messages)) if r.tool_name == name]


def filer(messages, info):
    """Reads the document, asks about the image, writes a file, then reports."""
    prompt = _last_prompt(messages)
    ids = IDS.findall(prompt)
    read, looked, made = (_returns(messages, n) for n in ("read_attachment", "look_at_attachment", "create_file"))
    if ids and not read:
        return ModelResponse(parts=[ToolCallPart("read_attachment", {"attachment_id": ids[0], "max_chars": 500})])
    if len(ids) > 1 and not looked:
        return ModelResponse(parts=[ToolCallPart("look_at_attachment", {"attachment_id": ids[1], "question": "What colour?"})])
    if not made:
        return ModelResponse(parts=[ToolCallPart("create_file", {"name": "summary.md", "content": "# Summary\nAll good."})])
    text = read[0].content.get("text", "")[:30] if read else ""
    return ModelResponse(parts=[TextPart(f"read={text!r} looked={looked[0].content if looked else None} "
                                         f"made={made[0].content['attachment']}")])


def eyes(messages, info):
    """A vision model: reports the colour of the image it was given."""
    for m in reversed(messages):
        for p in getattr(m, "parts", []):
            if p.part_kind == "user-prompt" and isinstance(p.content, list):
                img = next(c for c in p.content if isinstance(c, BinaryContent))
                with Image.open(io.BytesIO(img.data)) as im:
                    r, g, b = im.convert("RGB").getpixel((1, 1))
                return ModelResponse(parts=[TextPart("red" if r > 200 and g < 50 else "not red")])
    return ModelResponse(parts=[TextPart("no image")])


def multimodal(messages, info):
    """Counts the images in its own prompt and in the history it was given."""
    current = later = 0
    last = None
    for m in messages:
        for p in getattr(m, "parts", []):
            if p.part_kind == "user-prompt":
                last = p
    for m in messages:
        for p in getattr(m, "parts", []):
            if p.part_kind == "user-prompt" and isinstance(p.content, list):
                n = sum(isinstance(c, BinaryContent) for c in p.content)
                if p is last:
                    current += n
                else:
                    later += n
    SEEN.setdefault("multimodal", []).append((current, later, str(last.content)[:2000]))
    return ModelResponse(parts=[TextPart(f"images now={current} in history={later}")])


def passer(messages, info):
    """A boss that passes the user's file to the first team member."""
    prompt = _last_prompt(messages)
    returns = _returns(messages, "delegate")
    if returns:
        return ModelResponse(parts=[TextPart("Relayed: " + returns[0].content["summary"] + " | "
                                             + returns[0].content.get("details", "")[-120:])])
    team = re.findall(r"^- ([^:]+):", _instructions_of(messages).partition("Team members you can delegate to:")[2], re.M)
    return ModelResponse(parts=[ToolCallPart("delegate", {
        "agent_name": team[0], "task": "Summarise the attached file.", "attachment_ids": IDS.findall(prompt)[:1]})])


def reader_worker(messages, info):
    """Reads the file it was given, attaches a summary, returns a WorkResult."""
    prompt = _last_prompt(messages)
    read, made = _returns(messages, "read_attachment"), _returns(messages, "create_file")
    ids = IDS.findall(prompt)
    if ids and not read:
        return ModelResponse(parts=[ToolCallPart("read_attachment", {"attachment_id": ids[0]})])
    if not made:
        return ModelResponse(parts=[ToolCallPart("create_file", {"name": "notes.txt", "content": "short notes"})])
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
        "status": "done", "summary": "It says: " + read[0].content["text"][:20] if read else "nothing"})])


def bencher(messages, info):
    """Copies the file to its workbench, uppercases it there, and sends the result back."""
    tools = {t.name for t in info.function_tools}
    SEEN["bench_tools"] = sorted(tools)
    ids = IDS.findall(_last_prompt(messages))
    copied, ran, sent = (_returns(messages, n) for n in ("attachment_to_workbench", "bench_run_python", "send_from_workbench"))
    run = next(t for t in tools if t.endswith("run_python"))
    if not copied:
        return ModelResponse(parts=[ToolCallPart("attachment_to_workbench", {"attachment_id": ids[0]})])
    if not _returns(messages, run):
        path = copied[0].content["path"]
        return ModelResponse(parts=[ToolCallPart(run, {
            "code": f"import os; os.makedirs('out', exist_ok=True); open('out/UP.txt','w').write(open({path!r}).read().upper())"})])
    if not sent:
        return ModelResponse(parts=[ToolCallPart("send_from_workbench", {"path": "out/UP.txt"})])
    return ModelResponse(parts=[TextPart(f"copied={copied[0].content} sent={sent[0].content}")])


def _instructions_of(messages):
    for m in reversed(messages):
        if getattr(m, "instructions", None):
            return m.instructions
    return ""


SCRIPTS.update({"filer": filer, "eyes": eyes, "multimodal": multimodal, "passer": passer,
                "reader-worker": reader_worker, "bencher": bencher})


async def upload(hq, thread_id, *files):
    r = await hq.http.post(f"/api/threads/{thread_id}/attachments",
                           files=[("files", (n, d, t)) for n, d, t in files])
    assert r.status_code == 201, r.text
    return r.json()


async def send(hq, thread_id, text, ids):
    r = await hq.http.post(f"/api/threads/{thread_id}/messages",
                           json={"content": text, "attachment_ids": ids})
    assert r.status_code == 202, r.text


async def test_read_look_and_send_back(hq):
    await hq.start_worker()
    a = await hq.agent(name="Kit", model="fake:filer", vision_model="fake:eyes")
    t = await hq.thread(a["id"])
    up = await upload(hq, t["id"], ("notes.md", b"# UPS\nBattery at 96%.\n", "text/markdown"),
                      ("photo.png", png(), "image/png"))
    assert [f["kind"] for f in up] == ["text", "image"] and up[1]["width"] == 64
    await send(hq, t["id"], "What's in these?", [f["id"] for f in up])
    (reply,) = await hq.wait_agent_replies(t["id"], 1)
    assert "Battery at 96%" in reply and "looked=red" in reply

    msgs = hq.messages(t["id"])
    user = next(m for m in msgs if m.role == Role.user)
    assert [f["name"] for f in user.meta["attachments"]] == ["notes.md", "photo.png"]
    job = next(j for j in hq.jobs() if j.kind == "turn")
    assert "[Attached files]" in job.task and "look_at_attachment" in job.task and "Battery" in job.task

    agent_msg = next(m for m in msgs if m.role == Role.agent)
    (made,) = agent_msg.meta["attachments"]
    assert made["name"] == "summary.md" and made["source"] == "agent" and made["agent_id"] == a["id"]
    r = await hq.http.get(f"/api/attachments/{made['id']}")
    assert r.content == b"# Summary\nAll good." and r.headers["content-disposition"].startswith("attachment")

    listing = (await hq.http.get(f"/api/threads/{t['id']}/attachments")).json()
    assert {f["name"] for f in listing} == {"notes.md", "photo.png", "summary.md"}
    assert all(f["message_id"] for f in listing)


async def test_no_vision_model_says_so(hq):
    await hq.start_worker()
    a = await hq.agent(name="Kit", model="fake:filer")
    t = await hq.thread(a["id"])
    up = await upload(hq, t["id"], ("a.txt", b"hello", "text/plain"), ("b.png", png(), "image/png"))
    await send(hq, t["id"], "look", [f["id"] for f in up])
    (reply,) = await hq.wait_agent_replies(t["id"], 1)
    assert "no vision model" in reply


async def test_images_go_to_a_multimodal_model_and_leave_the_history(hq):
    SEEN.pop("multimodal", None)
    await hq.start_worker()
    a = await hq.agent(name="Iris", model="fake:multimodal", sees_images=True)
    t = await hq.thread(a["id"])
    big = png(size=(3000, 2000))
    up = await upload(hq, t["id"], ("big.png", big, "image/png"))
    await send(hq, t["id"], "", [up[0]["id"]])
    await hq.wait_agent_replies(t["id"], 1)
    await hq.say(t["id"], "and now?")
    replies = await hq.wait_agent_replies(t["id"], 2)
    assert replies == ["images now=1 in history=0", "images now=0 in history=0"]
    assert "shown to you with this message" in SEEN["multimodal"][0][2]
    assert "without a message" in SEEN["multimodal"][0][2]


async def test_files_travel_with_delegation(hq):
    await hq.start_worker()
    w = await hq.agent(name="Scout", model="fake:reader-worker", description="Reads things")
    b = await hq.agent(name="Chief", model="fake:passer", delegates=[w["id"]])
    t = await hq.thread(b["id"])
    up = await upload(hq, t["id"], ("plan.txt", b"Migrate the NAS on Friday.", "text/plain"))
    await send(hq, t["id"], "Have someone read this", [up[0]["id"]])
    (reply,) = await hq.wait_agent_replies(t["id"], 1)
    assert "It says: Migrate the NAS" in reply and "notes.txt" in reply

    task = next(j for j in hq.jobs() if j.kind == "task")
    assert task.meta["attachments"] == [up[0]["id"]] and "[Files for this task]" in task.task
    card = next(m for m in hq.messages(t["id"]) if m.meta.get("kind") == "delegation_result")
    assert [f["name"] for f in card.meta["given"]] == ["plan.txt"]
    assert [f["name"] for f in card.meta["attachments"]] == ["notes.txt"]


async def test_workbench_in_and_out(hq, tmp_path):
    await hq.start_worker()
    work = tmp_path / "bench"
    launch = ("import sys; sys.path.insert(0, %r); import workbench_mcp as w; "
              "w.WORK.mkdir(parents=True, exist_ok=True); w.ensure_venv(); w.mcp.run()") % str(WB_DIR)
    srv = (await hq.http.post("/api/tool-servers", json={
        "name": "bench", "transport": "stdio", "command": sys.executable, "args": ["-c", launch],
        "env": {"WORKBENCH_DIR": str(work)},
        "approval_rules": {"run_python": "allow"}})).json()
    a = await hq.agent(name="Flint", model="fake:bencher", tool_server_ids=[srv["id"]])
    t = await hq.thread(a["id"])
    up = await upload(hq, t["id"], ("data.csv", b"host,ok\nrewind,yes\n", "text/csv"))
    await send(hq, t["id"], "shout this", [up[0]["id"]])
    (reply,) = await hq.wait_agent_replies(t["id"], 1, timeout=60)
    assert (work / "inbox" / "data.csv").read_text() == "host,ok\nrewind,yes\n"
    assert not any("ahq_" in n for n in SEEN["bench_tools"])
    assert {"attachment_to_workbench", "send_from_workbench"} <= set(SEEN["bench_tools"])
    agent_msg = next(m for m in hq.messages(t["id"]) if m.role == Role.agent)
    (sent,) = agent_msg.meta["attachments"]
    assert sent["name"] == "UP.txt" and sent["source"] == "workbench"
    r = await hq.http.get(f"/api/attachments/{sent['id']}")
    assert r.content == b"HOST,OK\nREWIND,YES\n", reply


async def test_upload_rules_and_safe_downloads(hq, monkeypatch):
    a = await hq.agent(name="Kit", model="fake:echo")
    t1, t2 = await hq.thread(a["id"]), await hq.thread(a["id"])
    r = await hq.http.post(f"/api/threads/{t1['id']}/attachments", files=[("files", ("e.txt", b"", "text/plain"))])
    assert r.status_code == 422
    monkeypatch.setattr(settings, "attachment_max_bytes", 10)
    r = await hq.http.post(f"/api/threads/{t1['id']}/attachments", files=[("files", ("big.txt", b"x" * 11, "text/plain"))])
    assert r.status_code == 413
    monkeypatch.setattr(settings, "attachment_max_bytes", 1024 * 1024)
    r = await hq.http.post("/api/threads/nope/attachments", files=[("files", ("a.txt", b"x", "text/plain"))])
    assert r.status_code == 404

    page = b"<html><script>alert(1)</script></html>"
    (html,) = await upload(hq, t1["id"], ("page.html", page, "text/html"))
    (svg,) = await upload(hq, t1["id"], ("logo.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml"))
    assert svg["kind"] != "image" and not svg["mime"].startswith("image/")
    r = await hq.http.get(f"/api/attachments/{html['id']}")
    assert r.headers["content-disposition"].startswith("attachment")
    assert "sandbox" in r.headers["content-security-policy"] and r.headers["x-content-type-options"] == "nosniff"
    (img,) = await upload(hq, t1["id"], ("p.png", png(), "image/png"))
    r = await hq.http.get(f"/api/attachments/{img['id']}")
    assert r.headers["content-disposition"].startswith("inline") and r.headers["content-type"] == "image/png"
    assert (await hq.anon.get(f"/api/attachments/{img['id']}")).status_code == 401

    # A file from another conversation can't be sent here; an empty message needs a file.
    r = await hq.http.post(f"/api/threads/{t2['id']}/messages", json={"content": "x", "attachment_ids": [html["id"]]})
    assert r.status_code == 422
    r = await hq.http.post(f"/api/threads/{t2['id']}/messages", json={"content": " "})
    assert r.status_code == 422
    # Unsent files can be removed; sent ones can't, and can't be sent twice.
    assert (await hq.http.delete(f"/api/attachments/{svg['id']}")).status_code == 204
    await send(hq, t1["id"], "here", [html["id"]])
    assert (await hq.http.delete(f"/api/attachments/{html['id']}")).status_code == 409
    r = await hq.http.post(f"/api/threads/{t1['id']}/messages", json={"content": "again", "attachment_ids": [html["id"]]})
    assert r.status_code == 409
    text = (await hq.http.get(f"/api/attachments/{html['id']}/text")).json()
    assert text["name"] == "page.html" and "total_chars" in text  # HTML comes back as text, scripts dropped


@pytest.mark.parametrize("name,data,kind", [
    ("report.pdf", b"%PDF-1.4 not really", "document"),
    ("blob.bin", bytes(range(256)) * 4, "file"),
    ("script.py", b"print('hi')\n", "text"),
])
async def test_kinds(hq, name, data, kind):
    from app import attachments
    from app import db
    from app.models import AgentDef, Thread

    with db.session() as s:
        ag = AgentDef(name="K", model="fake:echo")
        s.add(ag)
        s.commit()
        th = Thread(agent_id=ag.id)
        s.add(th)
        s.commit()
        tid = th.id
    a = attachments.store(tid, name, data)
    assert a.kind == kind
    out = attachments.manifest([a])
    assert attachments.ref(a.id) in out
    if kind == "file":
        assert "no readable text" in attachments.read_text(a)["error"]


def test_bytes_round_trip_on_postgres(postgres_url, tmp_path, monkeypatch):
    """Files live in Postgres (bytea), so workers on other machines read the same bytes."""
    from app import attachments, db
    from app.config import reload_settings
    from app.models import AgentDef, Thread

    monkeypatch.setenv("AHQ_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AHQ_DB_URL", postgres_url)
    reload_settings()
    db.reset_engine()
    db.init_db()
    with db.session() as s:
        ag = AgentDef(name=f"PgFiles-{tmp_path.name}", model="fake:echo")
        s.add(ag)
        s.commit()
        th = Thread(agent_id=ag.id)
        s.add(th)
        s.commit()
        tid = th.id
    data = png(size=(40, 30))
    a = attachments.store(tid, "p.png", data)
    db.reset_engine()
    b = attachments.get(attachments.ref(a.id))
    assert b.data == data and b.kind == "image" and (b.width, b.height) == (40, 30)
