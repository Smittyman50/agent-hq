"""Each chat gets its own workbench folder, so a new chat starts from a clean slate.

Regression (2026-11-18): /work was one folder for every chat. Asked in a new chat to build
a game, H-Tech found the last chat's game there and spent 70 requests going over it.
"""

from __future__ import annotations

import base64
import importlib
import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

WB_DIR = Path(__file__).resolve().parents[2] / "workbench"


@pytest.fixture(scope="module")
def wb(tmp_path_factory):
    work = tmp_path_factory.mktemp("work")
    mp = pytest.MonkeyPatch()
    mp.setenv("WORKBENCH_DIR", str(work))
    mp.syspath_prepend(str(WB_DIR))
    sys.modules.pop("workbench_mcp", None)
    mod = importlib.import_module("workbench_mcp")
    mod.ensure_venv()
    yield mod
    mp.undo()
    sys.modules.pop("workbench_mcp", None)


@pytest.fixture(scope="module")
def wb_url(wb):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(wb.mcp.http_app(path="/mcp"), host="127.0.0.1",
                                           port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True


def _server(url, desktop=False):
    from app.models import ToolServer, Transport

    return ToolServer(name="workbench-1", transport=Transport.http, url=url, desktop=desktop)


async def _call(url, chat, tool, args):
    from app.runtime import mcp_toolset

    ts = mcp_toolset(_server(url), chat)
    async with ts:
        r = await ts.client.call_tool(tool, args, raise_on_error=False)
    text = " ".join(getattr(p, "text", "") for p in r.content).strip()
    try:
        return json.loads(text)
    except ValueError:
        return text


async def test_each_chat_sees_only_its_own_files(wb, wb_url):
    assert "wrote" in await _call(wb_url, "chat-a", "write_file", {"path": "game/main.py", "content": "print(1)"})
    assert "print(1)" in await _call(wb_url, "chat-a", "read_file", {"path": "game/main.py"})
    # a new chat starts empty: no game/ to find
    listing = await _call(wb_url, "chat-b", "list_files", {})
    assert not any("game" in str(x) for x in (listing if isinstance(listing, list) else [listing]))
    r = await _call(wb_url, "chat-b", "run_shell", {"command": "pwd; ls -A"})
    cwd, *files = r["stdout"].split()
    assert cwd.endswith("/chats/chat-b") and "game" not in files
    assert (wb.WORK / "chats" / "chat-a" / "game" / "main.py").exists()


async def test_the_chat_folder_is_the_whole_workspace(wb, wb_url):
    other = str(wb.WORK / "chats" / "chat-a" / "game" / "main.py")
    r = await _call(wb_url, "chat-b", "read_file", {"path": other})
    assert "outside your workspace" in str(r)
    r = await _call(wb_url, "chat-b", "read_file", {"path": "../chat-a/game/main.py"})
    assert "outside your workspace" in str(r)


async def test_venv_and_saved_scripts_carry_over_between_chats(wb, wb_url):
    r = await _call(wb_url, "chat-a", "run_python", {"code": "import sys; print(sys.prefix)"})
    assert r["stdout"].strip() == str(wb.VENV)
    await _call(wb_url, "chat-a", "save_script", {"name": "say_hi", "description": "hi", "code": "print('hi')"})
    assert "say_hi" in await _call(wb_url, "chat-b", "list_scripts", {})
    assert (await _call(wb_url, "chat-b", "run_script", {"name": "say_hi"}))["stdout"].strip() == "hi"


async def test_attachments_go_in_and_out_of_the_chats_folder(wb, wb_url):
    from app import attachments as att

    class A:
        name, data = "notes.txt", b"hello"

    srv = _server(wb_url)
    r = await att.to_workbench(srv, A(), "inbox/notes.txt", chat="chat-c")
    assert r["path"] == "inbox/notes.txt"
    assert (wb.WORK / "chats" / "chat-c" / "inbox" / "notes.txt").read_bytes() == b"hello"
    assert await att.fetch_file(srv, "inbox/notes.txt", chat="chat-c") == ("notes.txt", b"hello")
    with pytest.raises(Exception):
        await att.fetch_file(srv, "inbox/notes.txt", chat="chat-d")


async def test_no_chat_means_the_whole_workbench(wb, wb_url):
    # an older Agent HQ, or a client of your own, names no chat
    r = await _call(wb_url, None, "run_shell", {"command": "pwd"})
    assert r["stdout"].strip() == str(wb.WORK)
    r = await _call(wb_url, "../../etc", "run_shell", {"command": "pwd"})  # a bad id is ignored
    assert r["stdout"].strip() == str(wb.WORK)


def test_header_goes_to_workbenches_not_desktops():
    from app.runtime import CHAT_HEADER, mcp_toolset

    assert mcp_toolset(_server("http://wb/mcp"), "t1")._static_headers == {CHAT_HEADER: "t1"}
    assert not mcp_toolset(_server("http://desk/mcp", desktop=True), "t1")._static_headers
    assert not mcp_toolset(_server("http://wb/mcp"))._static_headers
