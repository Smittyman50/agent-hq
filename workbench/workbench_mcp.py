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
import keyword
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
    Use this, not run_shell with python - <<EOF, for any Python: the code goes in as it is,
    with no shell quoting to get wrong.
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
    """Run a bash command in your workspace (the venv is on PATH) and return its output.

    Keep it to short commands (ls, grep, wc, git, running a script you wrote). Never put
    code or file contents in the command (heredocs such as cat > f <<EOF or python - <<EOF,
    or long quoted strings): the quotes and backslashes inside them often make the tool
    call itself unreadable, and the call fails. Write files with write_file, run Python
    with run_python, or write a script with write_file and run it here.
    """
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


# --------------------------------------------------------------------------- scripts with tools
#
# run_with_tools (an Agent HQ tool) runs a script here that can call the agent's other
# tools. The script never reaches Agent HQ itself: its tool calls go into a queue that
# Agent HQ drains over this same MCP connection (ahq_script_poll), runs through the
# agent's own approval rules, and answers (ahq_script_reply). So the workbench needs no
# network path to Agent HQ, and its egress rules still apply to the script's own code.
# Between the script and this server, two pipes carry one JSON object per line.

SCRIPT_ABANDON_S = int(os.environ.get("WORKBENCH_SCRIPT_ABANDON", "180"))  # no poll for this long: kill
SCRIPT_MAX_CALLS = int(os.environ.get("WORKBENCH_SCRIPT_MAX_CALLS", "200"))

_STUB_HEAD = '''"""Your tools, for this script (generated by Agent HQ for this run).

Call them as functions, with the same arguments you would give the tool directly:
    vms = tools.labtools_proxmox_list_vms(node="pve1")
A call that fails, is denied or isn't allowed raises tools.ToolError. tools.result(value)
hands back a structured result along with what you print. tools.list_tools() prints them.
"""
import json as _json
import os as _os
import threading as _threading

_in = _os.fdopen(int(_os.environ["AHQ_TOOLS_IN"]), "r", encoding="utf-8")
_out = _os.fdopen(int(_os.environ["AHQ_TOOLS_OUT"]), "w", encoding="utf-8")
_lock = _threading.Lock()
_n = 0
_UNSET = object()


class ToolError(Exception):
    """A tool call failed, was denied by the user, or isn't allowed."""


def _call(_name, _args):
    global _n
    with _lock:
        _n += 1
        _out.write(_json.dumps({"type": "call", "id": _n, "tool": _name,
                                "args": {k: v for k, v in _args.items() if v is not _UNSET}},
                               default=str) + "\\n")
        _out.flush()
        line = _in.readline()
    if not line:
        raise ToolError("Agent HQ stopped answering tool calls")
    reply = _json.loads(line)
    if not reply.get("ok"):
        raise ToolError(reply.get("error") or (_name + " failed"))
    return reply.get("value")


def result(value):
    """Hand back a structured result (anything JSON can hold) along with the output."""
    with _lock:
        _out.write(_json.dumps({"type": "result", "value": value}, default=str) + "\\n")
        _out.flush()


def list_tools():
    """Print the tools this script can call."""
    for name, doc in sorted(TOOLS.items()):
        print(name + ": " + doc)

'''


def _py_name(name: str) -> str:
    out = re.sub(r"\W", "_", name)
    return ("t_" + out) if (not out or out[0].isdigit() or keyword.iskeyword(out)) else out


def _stub(tools: list[dict]) -> str:
    """The tools module for one run: a function per tool, named like the tool."""
    lines: list[str] = [_STUB_HEAD]
    listing: dict[str, str] = {}
    seen: set[str] = set()
    for t in tools:
        name = str(t.get("name") or "")
        if not name:
            continue
        fn = _py_name(name)
        if fn in seen or fn in ("result", "list_tools", "ToolError", "TOOLS"):
            continue
        seen.add(fn)
        schema = t.get("parameters") or {}
        props = schema.get("properties") or {}
        required = [k for k in schema.get("required") or [] if k in props]
        optional = [k for k in props if k not in required]
        doc = (t.get("description") or "").strip()
        listing[fn] = doc.split("\n", 1)[0][:200]
        if all(k.isidentifier() and not keyword.iskeyword(k) and not k.startswith("_") for k in props):
            sig = ", ".join([*required, *(f"{k}=_UNSET" for k in optional)])
            args = "{" + ", ".join(f"{k!r}: {k}" for k in props) + "}"
        else:  # argument names Python can't spell: take them as keywords
            sig, args = "**kwargs", "kwargs"
        arg_doc = "\n".join(
            f"    {k}: {(v or {}).get('description') or (v or {}).get('type') or ''}".rstrip()
            for k, v in props.items())
        body = doc + (("\n\nArgs:\n" + arg_doc) if arg_doc else "")
        lines.append(f"def {fn}({sig}):\n    {json.dumps(body)}\n    return _call({name!r}, {args})\n\n")
    lines.append(f"TOOLS = {listing!r}\n")
    return "\n".join(lines)


class _ScriptRun:
    def __init__(self, timeout: float, max_calls: int, max_output: int) -> None:
        self.id = os.urandom(6).hex()
        self.timeout, self.max_calls, self.max_output = timeout, max_calls, max_output
        self.proc: asyncio.subprocess.Process | None = None
        self.calls: asyncio.Queue = asyncio.Queue()
        self.result: Any = None
        self.has_result = False
        self.ncalls = 0
        self.active = 0.0                 # seconds the script itself has run
        self.running_since: float | None = time.monotonic()
        self.last_poll = time.monotonic()
        self.timed_out = False
        self.stop_reason = ""
        self.started = time.monotonic()
        self.writer: asyncio.StreamWriter | None = None
        self.done: asyncio.Task | None = None
        self.reader_task: asyncio.Task | None = None
        self.watch_task: asyncio.Task | None = None
        self.folder: Path | None = None

    def ran(self) -> float:
        now = time.monotonic()
        return self.active + ((now - self.running_since) if self.running_since is not None else 0.0)

    def pause(self) -> None:
        if self.running_since is not None:
            self.active += time.monotonic() - self.running_since
            self.running_since = None

    def resume(self) -> None:
        if self.running_since is None:
            self.running_since = time.monotonic()

    def kill(self, reason: str) -> None:
        self.stop_reason = self.stop_reason or reason
        if self.proc is not None and self.proc.returncode is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    async def send(self, msg: dict) -> None:
        assert self.writer is not None
        self.writer.write((json.dumps(msg, default=str) + "\n").encode())
        await self.writer.drain()


_script_runs: dict[str, _ScriptRun] = {}


async def _read_calls(run: _ScriptRun, reader: asyncio.StreamReader) -> None:
    while True:
        line = await reader.readline()
        if not line:
            return
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("type") == "result":
            run.result, run.has_result = msg.get("value"), True
        elif msg.get("type") == "call":
            if run.ncalls >= run.max_calls:
                await run.send({"id": msg.get("id"), "ok": False,
                                "error": f"this script has made its limit of {run.max_calls} tool calls"})
                continue
            run.ncalls += 1
            run.pause()  # waiting on Agent HQ (an approval, a slow tool) isn't the script's time
            await run.calls.put(msg)


async def _watch(run: _ScriptRun) -> None:
    while run.proc is not None and run.proc.returncode is None:
        if run.ran() > run.timeout:
            run.timed_out = True
            run.kill(f"stopped after {run.timeout:g} seconds of run time")
        elif time.monotonic() - run.last_poll > SCRIPT_ABANDON_S:
            run.kill("Agent HQ stopped collecting its tool calls")
        await asyncio.sleep(0.1)


@mcp.tool(meta=ASK)
async def ahq_script_start(code: str, tools: list[dict[str, Any]], timeout: int = DEFAULT_TIMEOUT,
                           max_calls: int = SCRIPT_MAX_CALLS, max_output: int = MAX_OUTPUT) -> dict:
    """(Agent HQ) Start a script that can call the agent's tools; returns its run_id."""
    run = _ScriptRun(timeout=max(1, min(int(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT)),
                     max_calls=max(0, min(int(max_calls), SCRIPT_MAX_CALLS)),
                     max_output=max(1000, min(int(max_output), MAX_OUTPUT)))
    run.folder = WORK / ".runs" / f"script_{run.id}"  # outside every chat's folder
    run.folder.mkdir(parents=True, exist_ok=True)
    (run.folder / "tools.py").write_text(_stub(tools))
    (run.folder / "main.py").write_text("import tools\n" + code)
    to_child_r, to_child_w = os.pipe()
    from_child_r, from_child_w = os.pipe()
    env = {**_env(), "AHQ_TOOLS_IN": str(to_child_r), "AHQ_TOOLS_OUT": str(from_child_w)}
    try:
        run.proc = await asyncio.create_subprocess_exec(
            str(VENV / "bin" / "python"), str(run.folder / "main.py"), cwd=_root(), env=env,
            start_new_session=True, pass_fds=(to_child_r, from_child_w),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
    except BaseException:
        for fd in (to_child_w, from_child_r):
            os.close(fd)
        raise
    finally:
        os.close(to_child_r)
        os.close(from_child_w)
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=16 * 1024 * 1024)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(from_child_r, "rb", 0))
    wt, wp = await loop.connect_write_pipe(asyncio.streams.FlowControlMixin, os.fdopen(to_child_w, "wb", 0))
    run.writer = asyncio.StreamWriter(wt, wp, None, loop)
    run.done = asyncio.create_task(run.proc.communicate())
    run.reader_task = asyncio.create_task(_read_calls(run, reader))
    run.watch_task = asyncio.create_task(_watch(run))
    _script_runs[run.id] = run
    return {"run_id": run.id}


async def _finish(run: _ScriptRun) -> dict:
    if run.reader_task is not None:  # take in a result sent just before the end
        try:
            await asyncio.wait_for(asyncio.shield(run.reader_task), 1.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
    out, err = (run.done.result() if run.done is not None and run.done.done()
                and not run.done.cancelled() and run.done.exception() is None else (b"", b""))
    for t in (run.reader_task, run.watch_task):
        if t is not None:
            t.cancel()
    if run.writer is not None:
        run.writer.close()
    _script_runs.pop(run.id, None)
    if run.folder is not None:
        import shutil
        shutil.rmtree(run.folder, ignore_errors=True)
    cap = run.max_output
    res: dict[str, Any] = {"state": "done",
                           "exit_code": None if run.stop_reason else (run.proc.returncode if run.proc else None),
                           "timed_out": run.timed_out, "seconds": round(run.ran(), 2),
                           "wall_seconds": round(time.monotonic() - run.started, 2),
                           "tool_calls": run.ncalls,
                           "stdout": _clip_to(out.decode(errors="replace"), cap),
                           "stderr": _clip_to(err.decode(errors="replace"), cap)}
    if run.stop_reason:
        res["stopped"] = run.stop_reason
    if run.has_result:
        text = json.dumps(run.result, default=str)
        res["result"] = run.result if len(text) <= cap else (
            text[:cap] + f"… [result cut: {len(text)} characters]")
    return res


def _clip_to(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    half = cap // 2
    return f"{text[:half]}\n… [{len(text) - cap} characters cut] …\n{text[-half:]}"


def _call_msg(msg: dict) -> dict:
    return {"state": "call", "call_id": msg.get("id"), "tool": msg.get("tool"), "args": msg.get("args") or {}}


@mcp.tool(meta=ALLOW, annotations={"readOnlyHint": True})
async def ahq_script_poll(run_id: str, wait: float = 10) -> dict:
    """(Agent HQ) The script's next tool call, its final output once it has ended, or
    "running" if neither happened within wait seconds."""
    run = _script_runs.get(run_id)
    if run is None:
        return {"state": "unknown", "error": "no such script run (it ended, or the workbench restarted)"}
    run.last_poll = time.monotonic()
    if not run.calls.empty():
        return _call_msg(run.calls.get_nowait())
    if not run.done.done():
        getter = asyncio.create_task(run.calls.get())
        finished, _ = await asyncio.wait({getter, run.done}, timeout=max(0.0, min(float(wait), 25.0)),
                                         return_when=asyncio.FIRST_COMPLETED)
        run.last_poll = time.monotonic()
        if getter in finished:
            return _call_msg(getter.result())
        getter.cancel()
        if run.done not in finished:
            return {"state": "running", "seconds": round(run.ran(), 2)}
    if not run.calls.empty():  # a last call raced the exit; the script is gone, so refuse it
        run.calls.get_nowait()
    return await _finish(run)


@mcp.tool(meta=ALLOW)
async def ahq_script_reply(run_id: str, call_id: int, ok: bool, value: Any = None, error: str = "") -> dict:
    """(Agent HQ) Answer the script's tool call."""
    run = _script_runs.get(run_id)
    if run is None:
        return {"state": "unknown"}
    run.resume()
    try:
        await run.send({"id": call_id, "ok": bool(ok), "value": value, "error": error})
    except (BrokenPipeError, ConnectionResetError):
        pass  # the script ended meanwhile; the next poll reports it
    return {"state": "ok"}


@mcp.tool(meta=ALLOW, annotations={"readOnlyHint": True})
async def ahq_script_ping(run_id: str) -> dict:
    """(Agent HQ) Still working on the script's tool call (an approval, a slow tool):
    don't take the script for abandoned."""
    run = _script_runs.get(run_id)
    if run is None:
        return {"state": "unknown"}
    run.last_poll = time.monotonic()
    return {"state": "ok"}


@mcp.tool(meta=ALLOW)
async def ahq_script_cancel(run_id: str) -> dict:
    """(Agent HQ) Stop a script (its turn was stopped)."""
    run = _script_runs.get(run_id)
    if run is None:
        return {"state": "unknown"}
    run.kill("stopped by Agent HQ")
    try:
        await asyncio.wait_for(asyncio.shield(run.done), 5)
    except asyncio.TimeoutError:
        pass
    return await _finish(run)


def ensure_venv() -> None:
    if not (VENV / "bin" / "python").exists():
        import venv

        venv.EnvBuilder(with_pip=True, system_site_packages=False).create(VENV)


if __name__ == "__main__":
    WORK.mkdir(parents=True, exist_ok=True)
    ensure_venv()
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("WORKBENCH_PORT", "8770")))
