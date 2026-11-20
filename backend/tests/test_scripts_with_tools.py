"""Scripts with tools (run_with_tools): a script on the agent's workbench calls its other
tools through Agent HQ's own rules, and only the script's output comes back to the model.

End to end: a real workbench and the lab MCP server (both over stdio), approvals decided
through the API. Runs on Postgres too (AHQ_TEST_DB=postgres)."""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import time
from pathlib import Path

import pytest
from pydantic_ai import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo

from .conftest import SCRIPTS, _tool_returns

WB_DIR = Path(__file__).resolve().parents[2] / "workbench"
LAB = Path(__file__).with_name("lab_mcp_server.py")

# The script the fake model runs: {list_vms} and {restart_vm} become the tools' real names.
CODE: dict[str, str] = {"code": "", "timeout": "120"}
SEEN: dict = {}


def scripter(messages, info: AgentInfo) -> ModelResponse:
    names = {t.name for t in info.function_tools}
    SEEN["tools"] = names
    if "run_with_tools" not in names:
        return ModelResponse(parts=[TextPart("I can't run scripts with tools.")])
    returns = _tool_returns(messages)
    SEEN["returned_tools"] = [r.tool_name for r in returns]
    done = [r for r in returns if r.tool_name == "run_with_tools"]
    if done:
        c = done[-1].content
        SEEN["result"] = c
        return ModelResponse(parts=[TextPart("Script: " + (json.dumps(c) if not isinstance(c, str) else c))])
    pick = lambda end: next((n for n in names if n.endswith(end)), end)  # noqa: E731
    code = CODE["code"].replace("{list_vms}", pick("list_vms")).replace("{restart_vm}", pick("restart_vm"))
    return ModelResponse(parts=[ToolCallPart("run_with_tools", {"code": code, "timeout": int(CODE["timeout"])})])


SCRIPTS["scripter"] = scripter


@pytest.fixture
def dirs(tmp_path):
    (tmp_path / "bench").mkdir()
    (tmp_path / "lab").mkdir()
    return tmp_path / "bench", tmp_path / "lab"


async def setup(hq, dirs, *, bench_rules=None, lab_rules=None, scripts=True, bench_env=None):
    bench, lab = dirs
    launch = ("import sys; sys.path.insert(0, %r); import workbench_mcp as w; "
              "w.WORK.mkdir(parents=True, exist_ok=True); w.ensure_venv(); w.mcp.run()") % str(WB_DIR)
    wb = (await hq.http.post("/api/tool-servers", json={
        "name": "bench", "transport": "stdio", "command": sys.executable, "args": ["-c", launch],
        "env": {"WORKBENCH_DIR": str(bench), "HTTPS_PROXY": "http://egress:3128", **(bench_env or {})},
        "approval_rules": bench_rules if bench_rules is not None else {"run_python": "allow"}})).json()
    labsrv = (await hq.http.post("/api/tool-servers", json={
        "name": "lab", "transport": "stdio", "command": sys.executable, "args": [str(LAB)],
        "env": {"LAB_DIR": str(lab)}, "approval_rules": lab_rules or {}})).json()
    a = await hq.agent(name="Scripty", model="fake:scripter", tool_server_ids=[wb["id"], labsrv["id"]],
                       scripts_with_tools=scripts)
    return a, await hq.thread(a["id"])


async def pending(hq, tool=None, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = (await hq.http.get("/api/tool-approvals?status=pending")).json()
        rows = [r for r in rows if tool is None or r["tool"] == tool]
        if rows:
            return rows[0]
        await asyncio.sleep(0.1)
    raise AssertionError(f"no pending approval for {tool}")


async def test_a_script_calls_several_tools_and_only_its_output_comes_back(hq, dirs):
    SEEN.clear()
    CODE.update(timeout="120", code=(
        "vms = tools.{list_vms}()\n"
        "restarted = [tools.{restart_vm}(name=v) for v in vms]\n"
        "print(f'{len(vms)} VMs, restarted {len(restarted)}')\n"
        "tools.result({'vms': vms})\n"))
    await hq.start_worker()
    a, t = await setup(hq, dirs)
    await hq.say(t["id"], "restart every VM")
    [reply] = await hq.wait_agent_replies(t["id"], 1, timeout=60)
    assert reply.startswith("Script:")
    r = SEEN["result"]
    assert r["stdout"].strip() == "2 VMs, restarted 2" and r["result"] == {"vms": ["web", "db"]}
    assert r["exit_code"] == 0 and r["tool_calls"] == 3
    # The model saw one tool result, the script's: not the three calls it made.
    assert SEEN["returned_tools"] == ["run_with_tools"]
    _, lab = dirs
    assert (lab / "listed").exists() and (lab / "restarted-web").exists() and (lab / "restarted-db").exists()
    # Each call is in the run's trace like a direct call (after the script tool itself).
    job = [j for j in hq.jobs() if j.origin_thread_id == t["id"]][-1]
    called = [step["tool"] for step in (job.meta or {}).get("trace", [])]
    assert called == ["run_with_tools", "lab_list_vms", "lab_restart_vm", "lab_restart_vm"]


async def test_a_non_read_only_call_in_a_script_raises_the_approval_card(hq, dirs):
    SEEN.clear()
    CODE.update(timeout="120", code=(
        "for v in tools.{list_vms}():\n"
        "    try:\n"
        "        print(tools.{restart_vm}(name=v))\n"
        "    except tools.ToolError as e:\n"
        "        print('refused', v)\n"))
    await hq.start_worker()
    a, t = await setup(hq, dirs, lab_rules={"restart_vm": "ask"})
    await hq.say(t["id"], "restart the VMs")
    first = await pending(hq, "restart_vm")
    assert first["args"] == {"name": "web"}
    card = [m for m in hq.messages(t["id"]) if (m.meta or {}).get("kind") == "tool_approval"]
    assert card and card[0].meta["tool"] == "restart_vm"
    await hq.http.post(f"/api/tool-approvals/{first['id']}/decide", json={"approve": True})
    second = await pending(hq, "restart_vm")
    assert second["args"] == {"name": "db"}
    await hq.http.post(f"/api/tool-approvals/{second['id']}/decide", json={"approve": False})
    await hq.wait_agent_replies(t["id"], 1, timeout=60)
    assert SEEN["result"]["stdout"].split("\n")[:2] == ["web restarted", "refused db"]
    _, lab = dirs
    assert (lab / "restarted-web").exists() and not (lab / "restarted-db").exists()


async def test_a_slow_approval_doesnt_get_the_script_killed(hq, dirs, monkeypatch):
    # The workbench kills a script nobody collects calls from (here after 2 s); waiting
    # on your decision longer than that must not count, so Agent HQ keeps pinging it.
    from app import runtime

    monkeypatch.setattr(runtime, "SCRIPT_KEEPALIVE_S", 0.5)
    SEEN.clear()
    CODE.update(timeout="120", code="print(tools.{restart_vm}(name='web'))\n")
    await hq.start_worker()
    a, t = await setup(hq, dirs, lab_rules={"restart_vm": "ask"}, bench_env={"WORKBENCH_SCRIPT_ABANDON": "2"})
    await hq.say(t["id"], "restart web")
    req = await pending(hq, "restart_vm")
    await asyncio.sleep(4)  # twice the abandon time
    await hq.http.post(f"/api/tool-approvals/{req['id']}/decide", json={"approve": True})
    await hq.wait_agent_replies(t["id"], 1, timeout=60)
    assert SEEN["result"]["stdout"].strip() == "web restarted" and "stopped" not in SEEN["result"]


async def test_blocked_tools_stay_blocked(hq, dirs):
    SEEN.clear()
    CODE.update(timeout="120", code=(
        "print('has restart:', hasattr(tools, '{restart_vm}'))\n"
        "tools.{restart_vm}(name='web')\n"))
    await hq.start_worker()
    a, t = await setup(hq, dirs, lab_rules={"restart_vm": "block"})
    await hq.say(t["id"], "restart web")
    await hq.wait_agent_replies(t["id"], 1, timeout=60)
    r = SEEN["result"]
    assert "has restart: False" in r["stdout"] and r["exit_code"] != 0 and "AttributeError" in r["stderr"]
    assert not (dirs[1] / "restarted-web").exists()


async def test_the_script_itself_follows_the_workbenchs_rule_for_running_code(hq, dirs):
    SEEN.clear()
    CODE.update(timeout="120", code="print(len(tools.{list_vms}()))\n")
    await hq.start_worker()
    # The workbench's own suggestion for run_python is ask: so the script asks first.
    a, t = await setup(hq, dirs, bench_rules={})
    await hq.say(t["id"], "count the VMs")
    req = await pending(hq, "run_with_tools")
    assert "list_vms" in req["args"]["code"]
    await hq.http.post(f"/api/tool-approvals/{req['id']}/decide", json={"approve": True})
    await hq.wait_agent_replies(t["id"], 1, timeout=60)
    assert SEEN["result"]["stdout"].strip() == "2"


async def test_running_code_blocked_means_no_scripts(hq, dirs):
    SEEN.clear()
    CODE.update(timeout="120", code="print(tools.{list_vms}())\n")
    await hq.start_worker()
    a, t = await setup(hq, dirs, bench_rules={"run_python": "block"})
    await hq.say(t["id"], "list")
    await hq.wait_agent_replies(t["id"], 1, timeout=60)
    assert "blocked running code" in SEEN["result"]["error"]
    assert not (dirs[1] / "listed").exists()


async def test_the_time_limit_counts_the_scripts_own_time(hq, dirs):
    SEEN.clear()
    CODE.update(timeout="2", code="tools.{list_vms}()\nwhile True:\n    pass\n")
    await hq.start_worker()
    a, t = await setup(hq, dirs)
    await hq.say(t["id"], "spin")
    await hq.wait_agent_replies(t["id"], 1, timeout=60)
    r = SEEN["result"]
    assert r["timed_out"] and "2 seconds of run time" in r["stopped"] and r["tool_calls"] == 1


async def test_stopping_the_turn_stops_the_script(hq, dirs):
    SEEN.clear()
    bench, _ = dirs
    CODE.update(timeout="120", code=(
        "import pathlib, time\n"
        f"beat = pathlib.Path({str(bench / 'beat')!r})\n"
        "tools.{list_vms}()\n"
        "while True:\n"
        "    beat.write_text(str(time.time()))\n"
        "    time.sleep(0.1)\n"))
    await hq.start_worker()
    a, t = await setup(hq, dirs)
    await hq.say(t["id"], "beat forever")
    for _ in range(300):
        if (bench / "beat").exists():
            break
        await asyncio.sleep(0.1)
    assert (bench / "beat").exists()
    await hq.http.post(f"/api/threads/{t['id']}/stop")
    await asyncio.sleep(2.5)
    last = (bench / "beat").read_text()
    await asyncio.sleep(1.0)
    assert (bench / "beat").read_text() == last  # no heartbeat since: the script is gone


async def test_off_unless_turned_on(hq, dirs):
    SEEN.clear()
    await hq.start_worker()
    a, t = await setup(hq, dirs, scripts=False)
    await hq.say(t["id"], "anything")
    assert await hq.wait_agent_replies(t["id"], 1, timeout=30) == ["I can't run scripts with tools."]


# -- the workbench side on its own ---------------------------------------------------------

@pytest.fixture
def wb(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_DIR", str(tmp_path))
    monkeypatch.setenv("HTTPS_PROXY", "http://egress:3128")
    monkeypatch.syspath_prepend(str(WB_DIR))
    sys.modules.pop("workbench_mcp", None)
    mod = importlib.import_module("workbench_mcp")
    mod.ensure_venv()
    yield lambda name: getattr(getattr(mod, name), "fn", getattr(mod, name))
    sys.modules.pop("workbench_mcp", None)


async def drive(wb, code, tools=(), answer=None, **kw):
    """Run a script on the workbench, answering its calls with answer(tool, args)."""
    run = (await wb("ahq_script_start")(code, list(tools), **kw))["run_id"]
    while True:
        p = await wb("ahq_script_poll")(run, wait=5)
        if p["state"] == "call":
            ok, value = answer(p["tool"], p["args"])
            await wb("ahq_script_reply")(run, p["call_id"], ok, value=value if ok else None,
                                          error="" if ok else value)
        elif p["state"] == "done":
            return p


TOOLS = [{"name": "lab_list-vms", "description": "List VMs.\nLonger text.",
          "parameters": {"type": "object", "properties": {"node": {"type": "string"}}}},
         {"name": "odd", "description": "Odd argument names",
          "parameters": {"type": "object", "properties": {"for": {"type": "string"}}, "required": ["for"]}}]


async def test_the_tools_module(wb):
    p = await drive(wb, (
        "tools.list_tools()\n"
        "print(tools.lab_list_vms(node='pve1'))\n"
        "print(tools.odd(**{'for': 'x'}))\n"
        "try:\n    tools.lab_list_vms(node='nope')\nexcept tools.ToolError as e:\n    print('error:', e)\n"),
        TOOLS, answer=lambda tool, args: (args.get("node") != "nope", args if args.get("node") != "nope" else "denied"))
    out = p["stdout"].splitlines()
    assert out[0] == "lab_list_vms: List VMs." and out[1] == "odd: Odd argument names"
    assert out[2] == "{'node': 'pve1'}" and out[3] == "{'for': 'x'}" and out[4] == "error: denied"


async def test_tool_call_limit_and_output_cap(wb):
    p = await drive(wb, (
        "n = 0\n"
        "try:\n    while True:\n        tools.lab_list_vms(); n += 1\n"
        "except tools.ToolError as e:\n    print(n, e)\n"
        "print('x' * 5000)\n"), TOOLS, answer=lambda t, a: (True, []), max_calls=3, max_output=1000)
    assert p["stdout"].startswith("3 this script has made its limit of 3 tool calls")
    assert "characters cut" in p["stdout"] and p["tool_calls"] == 3


async def test_the_script_runs_with_the_workbenchs_network_settings(wb):
    # Its own code goes out (or doesn't) exactly as run_python's would: same environment,
    # same container, so the egress proxy and its rules apply.
    p = await drive(wb, "import os\nprint(os.environ.get('HTTPS_PROXY'))\n")
    assert p["stdout"].strip() == "http://egress:3128"
