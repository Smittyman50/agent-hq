"""MCP server for an agent's workbench: its own Python venv, files and shell.

Runs in a `workbench-N` container (see the Dockerfile) at http://workbench-N:8770/mcp.
Register that URL in Agent HQ (Settings → Tools) and tick it on the agent that owns it.

Everything lives in /work, a persistent volume: files the agent writes, the virtual
environment at /work/.venv (packages from pip_install survive restarts), and saved
scripts in /work/scripts that the agent can run again later by name.

The container is the sandbox: it runs as an unprivileged user with a read-only root
filesystem and, by default, no network access except PyPI through the egress proxy.
Tools that run code declare that they should be approved first ("agent_hq/approval":
"ask" in their MCP metadata); you can change that per tool in Agent HQ.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import time
from pathlib import Path

from fastmcp import FastMCP

WORK = Path(os.environ.get("WORKBENCH_DIR", "/work")).resolve()
VENV = WORK / ".venv"
SCRIPTS = WORK / "scripts"
MAX_OUTPUT = int(os.environ.get("WORKBENCH_MAX_OUTPUT", "20000"))
DEFAULT_TIMEOUT = int(os.environ.get("WORKBENCH_TIMEOUT", "120"))
MAX_TIMEOUT = int(os.environ.get("WORKBENCH_MAX_TIMEOUT", "1800"))
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")

ASK = {"agent_hq/approval": "ask"}
ALLOW = {"agent_hq/approval": "allow"}

mcp = FastMCP("workbench", instructions=(
    "Your own Linux workbench with a persistent Python environment. Paths are relative to "
    "your workspace. Write code to files, run it, and read the output. Packages you install "
    "with pip_install stay installed. Save scripts you'll want again with save_script. "
    "There is no internet access unless the user has turned it on; PyPI works for installs."))


def _path(p: str) -> Path:
    """A path inside the workspace (relative paths are relative to it)."""
    full = (WORK / p).resolve() if not os.path.isabs(p) else Path(p).resolve()
    if full != WORK and WORK not in full.parents:
        raise ValueError(f"{p} is outside your workspace")
    return full


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(HOME=str(WORK), VIRTUAL_ENV=str(VENV), PYTHONUNBUFFERED="1",
               PATH=f"{VENV / 'bin'}:{env.get('PATH', '/usr/bin:/bin')}")
    return env


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    half = MAX_OUTPUT // 2
    return f"{text[:half]}\n… [{len(text) - MAX_OUTPUT} characters cut] …\n{text[-half:]}"


async def _run(argv: list[str], timeout: int, stdin: str | None = None,
               extra_env: dict[str, str] | None = None) -> dict:
    timeout = max(1, min(int(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=WORK, env={**_env(), **(extra_env or {})}, start_new_session=True,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    timed_out = False
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(stdin.encode() if stdin is not None else None), timeout)
    except asyncio.TimeoutError:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # the whole process group, children included
        except ProcessLookupError:
            pass
        out, err = await proc.communicate()
    return {"exit_code": None if timed_out else proc.returncode,
            "timed_out": timed_out, "seconds": round(time.monotonic() - started, 2),
            "stdout": _clip(out.decode(errors="replace")),
            "stderr": _clip(err.decode(errors="replace"))}


@mcp.tool(meta=ASK)
async def run_python(code: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Run Python code in your workbench's virtual environment and return its output.

    The working directory is your workspace, so files you write stay there. Print what you
    want to see. timeout is in seconds (the process and its children are killed after it).
    """
    runs = WORK / ".runs"
    runs.mkdir(exist_ok=True)
    f = runs / f"run_{int(time.time() * 1000)}.py"
    f.write_text(code)
    try:
        return await _run([str(VENV / "bin" / "python"), str(f)], timeout)
    finally:
        f.unlink(missing_ok=True)


@mcp.tool(meta=ASK)
async def run_shell(command: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Run a bash command in your workspace (the venv is on PATH) and return its output."""
    return await _run(["bash", "-lc", command], timeout)


@mcp.tool(meta=ASK)
async def pip_install(packages: list[str], upgrade: bool = False) -> dict:
    """Install Python packages into your workbench's virtual environment (they persist)."""
    bad = [p for p in packages if not re.match(r"^[A-Za-z0-9_.\-\[\],<>=!~ ]+$", p)]
    if bad or not packages:
        raise ValueError(f"not package names: {bad or packages}")
    argv = [str(VENV / "bin" / "pip"), "install", "--disable-pip-version-check", "--no-input"]
    return await _run(argv + (["--upgrade"] if upgrade else []) + packages, 600)


@mcp.tool(meta=ALLOW, annotations={"readOnlyHint": True})
def read_file(path: str, max_chars: int = MAX_OUTPUT) -> str:
    """Read a text file from your workspace."""
    text = _path(path).read_text(errors="replace")
    return text if len(text) <= max_chars else text[:max_chars] + f"\n… [{len(text) - max_chars} more characters]"


@mcp.tool(meta=ALLOW)
def write_file(path: str, content: str, append: bool = False) -> str:
    """Write (or append to) a text file in your workspace, creating folders as needed."""
    p = _path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a" if append else "w") as fh:
        fh.write(content)
    return f"{'appended' if append else 'wrote'} {len(content)} characters to {p.relative_to(WORK)}"


@mcp.tool(meta=ALLOW, annotations={"readOnlyHint": True})
def list_files(path: str = ".", depth: int = 2) -> list[str]:
    """List files in your workspace (folders end with /). Hidden folders are skipped."""
    root = _path(path)
    out: list[str] = []

    def walk(d: Path, level: int) -> None:
        for child in sorted(d.iterdir()):
            if child.name.startswith(".") or len(out) >= 500:
                continue
            rel = str(child.relative_to(WORK))
            if child.is_dir():
                out.append(rel + "/")
                if level < depth:
                    walk(child, level + 1)
            else:
                out.append(f"{rel}  ({child.stat().st_size} bytes)")

    walk(root, 1)
    return out


def _index() -> dict:
    f = SCRIPTS / "index.json"
    return json.loads(f.read_text()) if f.exists() else {}


@mcp.tool(meta=ALLOW)
def save_script(name: str, description: str, code: str, arguments: str = "") -> str:
    """Save a Python script you'll want again, as a named tool you can run with run_script.

    The script gets its arguments as a JSON object: `import json, os;
    args = json.loads(os.environ["ARGS"])`. Describe the arguments it expects in
    `arguments`. Saving under an existing name replaces that script.
    """
    if not NAME_RE.match(name):
        raise ValueError("use a lowercase name like fetch_ups_status (letters, digits, _)")
    SCRIPTS.mkdir(exist_ok=True)
    (SCRIPTS / f"{name}.py").write_text(code)
    idx = _index()
    idx[name] = {"description": description.strip(), "arguments": arguments.strip(),
                 "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    (SCRIPTS / "index.json").write_text(json.dumps(idx, indent=2))
    return f"saved script {name}; run it with run_script(name={name!r}, args={{...}})"


@mcp.tool(meta=ALLOW, annotations={"readOnlyHint": True})
def list_scripts() -> dict:
    """Your saved scripts: name → description and the arguments each expects."""
    return _index()


@mcp.tool(meta=ASK)
async def run_script(name: str, args: dict | None = None, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Run one of your saved scripts with arguments (passed as JSON in $ARGS)."""
    if name not in _index():
        raise ValueError(f"no saved script {name!r}; see list_scripts")
    return await _run([str(VENV / "bin" / "python"), str(SCRIPTS / f"{name}.py")], timeout,
                      extra_env={"ARGS": json.dumps(args or {})})


def ensure_venv() -> None:
    if not (VENV / "bin" / "python").exists():
        import venv

        venv.EnvBuilder(with_pip=True, system_site_packages=False).create(VENV)


if __name__ == "__main__":
    WORK.mkdir(parents=True, exist_ok=True)
    ensure_venv()
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("WORKBENCH_PORT", "8770")))
