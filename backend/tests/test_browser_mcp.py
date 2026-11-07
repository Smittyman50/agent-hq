"""Text-only web browsing: an agent drives a real Chromium through Playwright MCP attached
over CDP (as in the desktop container), reading the page as an accessibility snapshot.
Skipped when Chrome/Chromium or npx is not available."""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest
from pydantic_ai import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo

from .conftest import SCRIPTS, _last_prompt, _tool_returns

PAGE = """<html><head><title>Zabbix (stand-in)</title></head><body>
<h1>Problems</h1><table><tr><th>Host</th><th>Problem</th></tr>
<tr><td>pve1</td><td>Disk 95% full</td></tr></table>
<button onclick="document.getElementById('s').textContent='Acknowledged'">Acknowledge</button>
<p id="s"></p></body></html>"""


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(url: str, seconds: float) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except Exception as e:
            if getattr(e, "code", None):  # any HTTP answer means it is up
                return True
            time.sleep(0.3)
    return False


@pytest.fixture(scope="module")
def browser_mcp():
    chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    npx = shutil.which("npx")
    if not chrome or not npx:
        pytest.skip("Chrome/Chromium or npx not installed")
    tmp = Path(tempfile.mkdtemp(prefix="ahq-web-"))
    (tmp / "index.html").write_text(PAGE)
    web_port, cdp_port, mcp_port = _port(), _port(), _port()
    procs = [
        subprocess.Popen(["python3", "-m", "http.server", str(web_port)], cwd=tmp,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
        subprocess.Popen([chrome, "--headless=new", "--no-sandbox", f"--remote-debugging-port={cdp_port}",
                          f"--user-data-dir={tmp / 'profile'}", "about:blank"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
    ]
    if not _wait(f"http://127.0.0.1:{cdp_port}/json/version", 15):
        pytest.skip("Chrome did not start")
    version = os.environ.get("AHQ_TEST_PLAYWRIGHT_MCP", "0.0.84")
    procs.append(subprocess.Popen(
        [npx, "-y", f"@playwright/mcp@{version}", "--cdp-endpoint", f"http://127.0.0.1:{cdp_port}",
         "--host", "127.0.0.1", "--port", str(mcp_port), "--output-dir", str(tmp / "out"),
         "--codegen", "none"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    if not _wait(f"http://127.0.0.1:{mcp_port}/mcp", 60):
        for p in procs:
            p.terminate()
        pytest.skip("Playwright MCP did not start (offline?)")
    yield {"mcp": f"http://127.0.0.1:{mcp_port}/mcp", "site": f"http://127.0.0.1:{web_port}/"}
    for p in procs:
        p.terminate()
    shutil.rmtree(tmp, ignore_errors=True)


def web_user(messages, info: AgentInfo) -> ModelResponse:
    """Navigate, read the page as text, click Acknowledge by its ref, read again."""
    returns = _tool_returns(messages)  # newest first
    calls = len(returns)
    if calls == 0:
        url = re.search(r"https?://\S+", _last_prompt(messages)).group(0)
        return ModelResponse(parts=[ToolCallPart("browser_navigate", {"url": url})])
    if calls == 1:
        return ModelResponse(parts=[ToolCallPart("browser_snapshot", {})])
    if calls == 2:
        ref = re.search(r'button "Acknowledge" \[ref=(\w+)\]', str(returns[0].content)).group(1)
        return ModelResponse(parts=[ToolCallPart("browser_click", {"target": ref, "element": "Acknowledge button"})])
    if calls == 3:
        return ModelResponse(parts=[ToolCallPart("browser_snapshot", {})])
    snap = str(returns[0].content)
    problem = re.search(r'cell "(Disk[^"]+)"', snap).group(1)
    status = "acknowledged" if "Acknowledged" in snap else "not acknowledged"
    return ModelResponse(parts=[TextPart(f"pve1: {problem}; {status}")])


SCRIPTS["web-user"] = web_user


async def test_text_only_browsing_through_playwright_mcp(hq, browser_mcp):
    await hq.start_worker()
    srv = (await hq.http.post("/api/tool-servers", json={
        "name": "browser", "transport": "http", "url": browser_mcp["mcp"],
        "prefix_tools": False, "desktop": True})).json()
    probe = (await hq.http.get(f"/api/tool-servers/{srv['id']}/tools")).json()
    assert {"browser_navigate", "browser_snapshot", "browser_click"} <= {t["name"] for t in probe}
    a = await hq.agent(name="Web", model="fake:web-user", tool_server_ids=[srv["id"]])
    t = await hq.thread(a["id"])
    await hq.say(t["id"], f"Check {browser_mcp['site']} and acknowledge the problem")
    assert await hq.wait_agent_replies(t["id"], 1, timeout=60) == ["pve1: Disk 95% full; acknowledged"]
