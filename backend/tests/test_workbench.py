"""The workbench MCP server: run code, files, saved scripts, confinement, timeouts."""

from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest

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


async def test_run_python_in_its_own_venv_and_workspace(wb):
    r = await wb.run_python("import sys, os; print(sys.prefix); print(os.getcwd()); open('out.txt','w').write('hi')")
    assert r["exit_code"] == 0 and not r["timed_out"]
    prefix, cwd = r["stdout"].split()
    assert prefix == str(wb.VENV) and cwd == str(wb.WORK)
    assert wb.read_file("out.txt") == "hi"


async def test_errors_and_timeouts(wb):
    r = await wb.run_python("raise SystemExit('boom')")
    assert r["exit_code"] == 1 and "boom" in r["stderr"]
    started = time.monotonic()
    r = await wb.run_shell("sleep 30 & sleep 30; echo never", timeout=1)
    assert r["timed_out"] and r["exit_code"] is None and time.monotonic() - started < 10


async def test_output_is_capped(wb, monkeypatch):
    monkeypatch.setattr(wb, "MAX_OUTPUT", 1000)
    r = await wb.run_python("print('x' * 50000)")
    assert len(r["stdout"]) < 1200 and "characters cut" in r["stdout"]


def test_files_stay_inside_the_workspace(wb):
    assert "wrote" in wb.write_file("notes/a.md", "# hello")
    assert "notes/" in wb.list_files() and any(f.startswith("notes/a.md") for f in wb.list_files())
    for bad in ("../escape.txt", "/etc/passwd", "notes/../../x"):
        with pytest.raises(ValueError):
            wb.read_file(bad)
        with pytest.raises(ValueError):
            wb.write_file(bad, "x")


async def test_saved_scripts_are_reusable_tools(wb):
    code = "import json, os\nargs = json.loads(os.environ['ARGS'])\nprint(args['a'] * args['b'])"
    wb.save_script("multiply", "Multiplies a and b", code, arguments="a: number, b: number")
    assert wb.list_scripts()["multiply"]["description"] == "Multiplies a and b"
    r = await wb.run_script("multiply", {"a": 6, "b": 7})
    assert r["stdout"].strip() == "42"
    with pytest.raises(ValueError):
        wb.save_script("Bad Name!", "x", "print(1)")
    with pytest.raises(ValueError):
        await wb.run_script("nope")


async def test_pip_install_rejects_non_package_arguments(wb):
    with pytest.raises(ValueError):
        await wb.pip_install(["requests; rm -rf /"])


async def test_code_tools_ask_for_approval_by_default(wb):
    from fastmcp import Client

    async with Client(wb.mcp) as c:
        tools = {t.name: (t.meta or {}).get("agent_hq/approval") for t in await c.list_tools()}
    assert tools["run_python"] == tools["run_shell"] == tools["pip_install"] == tools["run_script"] == "ask"
    assert tools["read_file"] == tools["write_file"] == tools["list_files"] == "allow"
