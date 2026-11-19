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
from typing import Any

from fastmcp import FastMCP

WORK = Path(os.environ.get("WORKBENCH_DIR", "/work")).resolve()
VENV = WORK / ".venv"
SCRIPTS = WORK / "scripts"
MAX_OUTPUT = int(os.environ.get("WORKBENCH_MAX_OUTPUT", "20000"))
DEFAULT_TIMEOUT = int(os.environ.get("WORKBENCH_TIMEOUT", "120"))
MAX_TIMEOUT = int(os.environ.get("WORKBENCH_MAX_TIMEOUT", "1800"))
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
# Agent HQ names the chat each call belongs to in this header; each chat gets its own
# folder (chats/<id>), so a new chat starts empty instead of finding the last one's files.
# The venv and saved scripts stay shared: those are meant to carry over.
CHAT_HEADER = "x-agenthq-chat"
CHATS = WORK / "chats"
CHAT_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

ASK = {"agent_hq/approval": "ask"}
ALLOW = {"agent_hq/approval": "allow"}

mcp = FastMCP("workbench", instructions=(
    "Your own Linux workbench with a persistent Python environment. Each chat has its own "
    "workspace folder, empty when the chat starts; paths are relative to it. Write code to files, run it, and read the output. Packages you install "
    "with pip_install stay installed. Save scripts you'll want again with save_script. "
    "There is no internet access unless the user has turned it on; PyPI works for installs."))


def _chat() -> str | None:
    """The chat this call is for, from Agent HQ's header (None outside a request)."""
    try:
        from fastmcp.server.dependencies import get_http_headers

        chat = (get_http_headers(include={CHAT_HEADER}) or {}).get(CHAT_HEADER, "").strip()
    except Exception:
        return None
    return chat if CHAT_RE.match(chat) else None


def _root() -> Path:
    """This call's workspace: the chat's own folder, or the whole workbench when the
    caller names no chat (an older Agent HQ, or a client of your own)."""
    chat = _chat()
    if not chat:
        return WORK
    root = CHATS / chat
    root.mkdir(parents=True, exist_ok=True)
    return root


def _path(p: str) -> Path:
    """A path inside the workspace (relative paths are relative to it)."""
    root = _root()
    full = (root / p).resolve() if not os.path.isabs(p) else Path(p).resolve()
    if full != root and root not in full.parents:
        raise ValueError(f"{p} is outside your workspace")
    return full


def _rel(p: Path) -> str:
    return str(p.relative_to(_root()))


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(HOME=str(_root()), VIRTUAL_ENV=str(VENV), PYTHONUNBUFFERED="1",
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
        *argv, cwd=_root(), env={**_env(), **(extra_env or {})}, start_new_session=True,
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
    runs = WORK / ".runs"  # outside every chat's folder; the file is deleted afterwards
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
def read_file(path: str, max_chars: int = MAX_OUTPUT, start_line: int = 0, end_line: int = 0) -> str:
    """Read a text file from your workspace.

    To read part of a long file, give start_line and end_line (1-based, inclusive); those
    lines come back numbered, which helps you pick the exact text for edit_file.
    """
    text = _path(path).read_text(errors="replace")
    if start_line or end_line:
        lines = text.splitlines()
        first = max(1, start_line or 1)
        last = min(len(lines), end_line or len(lines))
        text = "\n".join(f"{n:>5}  {lines[n - 1]}" for n in range(first, last + 1))
        text += f"\n[lines {first}-{last} of {len(lines)}]"
    return text if len(text) <= max_chars else text[:max_chars] + f"\n… [{len(text) - max_chars} more characters]"


@mcp.tool(meta=ALLOW)
def write_file(path: str, content: str, append: bool = False) -> str:
    """Write (or append to) a text file in your workspace, creating folders as needed.

    To change part of a file that already exists, use edit_file instead of writing the
    whole file again. Write a big new file in parts: the first part, then append the rest.
    """
    p = _path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a" if append else "w") as fh:
        fh.write(content)
    return f"{'appended' if append else 'wrote'} {len(content)} characters to {_rel(p)}"


def _near(text: str, old: str) -> str:
    """Where text most like `old` is, for an edit that didn't match exactly."""
    import difflib

    lines = text.splitlines()
    want = next((l.strip() for l in old.splitlines() if l.strip()), "")
    if not want or not lines:
        return ""
    best = difflib.get_close_matches(want, [l.strip() for l in lines], n=1, cutoff=0.6)
    if not best:
        return ""
    n = [l.strip() for l in lines].index(best[0]) + 1
    shown = "\n".join(f"{i:>5}  {lines[i - 1]}" for i in range(max(1, n - 2), min(len(lines), n + 2) + 1))
    return f" The closest text is around line {n}:\n{shown}\nCopy the text exactly, spaces and indentation included."


@mcp.tool(meta=ALLOW)
def edit_file(path: str, old_text: str = "", new_text: str = "", replace_all: bool = False,
              edits: list[dict[str, Any]] | None = None) -> str:
    """Change part of a text file: replace old_text with new_text.

    old_text must match the file exactly (spaces, indentation and line breaks included) and,
    unless replace_all is true, occur exactly once: include a few lines around the change
    to make it unique. An empty new_text deletes old_text. Much cheaper than rewriting the
    whole file with write_file.

    To make several changes to the file in one call, pass edits instead: a list of
    {"old_text": ..., "new_text": ..., "replace_all": false}, applied in order, each to the
    result of the one before. Either all of them apply or none do. One call with ten edits
    costs one step; ten calls cost ten.
    """
    p = _path(path)
    if not p.is_file():
        raise ValueError(f"{path} doesn't exist; create it with write_file")
    if edits is not None and old_text:
        raise ValueError("pass either old_text/new_text or edits, not both")
    steps = edits if edits is not None else [{"old_text": old_text, "new_text": new_text,
                                               "replace_all": replace_all}]
    if not steps:
        raise ValueError("edits is empty")
    original = text = p.read_text(errors="replace")
    done: list[tuple[int, int, int]] = []  # (first line, lines in new text, occurrences) per edit
    for n, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            raise ValueError(f"edit {n} isn't an object with old_text and new_text")
        old, new = step.get("old_text") or "", step.get("new_text") or ""
        every = bool(step.get("replace_all", False))
        which = f"edit {n} of {len(steps)}: " if len(steps) > 1 else ""
        undone = " Nothing was changed: fix that edit and send them all again." if len(steps) > 1 else ""
        if not old:
            raise ValueError(f"{which}old_text is empty; to add to the end of a file, use write_file "
                             "with append" + undone)
        count = text.count(old)
        if count == 0:
            raise ValueError(f"{which}old_text isn't in {path}" + (" (after the edits before it)" if n > 1 else "")
                             + "." + _near(text, old) + undone)
        if count > 1 and not every:
            raise ValueError(f"{which}old_text occurs {count} times in {path}; add surrounding lines to "
                             "make it unique, or set replace_all to change every one." + undone)
        start = text.index(old)
        text = text.replace(old, new) if every else text.replace(old, new, 1)
        done.append((text.count("\n", 0, start) + 1, new.count("\n"), count if every else 1))
    if text != original:
        p.write_text(text)
    lines = text.splitlines()
    if len(done) == 1:
        first, extra, count = done[0]
        last = min(len(lines), first + extra)
        shown = "\n".join(f"{i:>5}  {lines[i - 1]}" for i in range(max(1, first - 1), min(len(lines), last + 1) + 1))
        what = f"{count} occurrences" if count > 1 else "1 occurrence"
        return f"replaced {what} in {_rel(p)}. Now around line {first}:\n{shown}"
    where = ", ".join(f"line {first}" for first, _, _ in done)
    return (f"applied {len(done)} edits to {_rel(p)} (now {len(lines)} lines), at {where}. "
            "Read the file back if you need to check them.")


@mcp.tool(meta=ALLOW, annotations={"readOnlyHint": True})
def list_files(path: str = ".", depth: int = 2) -> list[str]:
    """List files in your workspace (folders end with /). Hidden folders are skipped."""
    root, top = _path(path), _root()
    out: list[str] = []

    def walk(d: Path, level: int) -> None:
        for child in sorted(d.iterdir()):
            if child.name.startswith(".") or len(out) >= 500:
                continue
            rel = str(child.relative_to(top))
            if child.is_dir():
                out.append(rel + "/")
                if level < depth:
                    walk(child, level + 1)
            else:
                out.append(f"{rel}  ({child.stat().st_size} bytes)")

    walk(root, 1)
    return out


# Agent HQ moves chat attachments in and out with these two. They are hidden from the
# agent itself (Agent HQ drops ahq_* tools from its tool list); agents use
# attachment_to_workbench / send_from_workbench, which call them.
MAX_TRANSFER = int(os.environ.get("WORKBENCH_MAX_TRANSFER", str(25 * 1024 * 1024)))


@mcp.tool(meta=ALLOW)
def ahq_put_file(path: str, data_b64: str) -> str:
    """(Agent HQ) Write a file from base64, creating folders as needed."""
    import base64

    data = base64.b64decode(data_b64)
    if len(data) > MAX_TRANSFER:
        raise ValueError(f"file is larger than {MAX_TRANSFER} bytes")
    p = _path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return json.dumps({"path": _rel(p), "bytes": len(data)})


@mcp.tool(meta=ALLOW, annotations={"readOnlyHint": True})
def ahq_get_file(path: str, max_bytes: int = MAX_TRANSFER) -> str:
    """(Agent HQ) Read a file as base64."""
    import base64

    p = _path(path)
    if not p.is_file():
        return json.dumps({"error": f"no file {path} in your workspace"})
    size = p.stat().st_size
    if size > min(max_bytes, MAX_TRANSFER):
        return json.dumps({"error": f"{path} is {size} bytes, more than the {min(max_bytes, MAX_TRANSFER)} allowed"})
    return json.dumps({"name": p.name, "bytes": size,
                       "data_b64": base64.b64encode(p.read_bytes()).decode()})


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
