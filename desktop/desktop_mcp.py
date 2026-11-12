"""MCP server that lets an agent see and drive the Linux desktop it runs inside.

Runs inside each agent's desktop container (see Dockerfile) and listens on
http://0.0.0.0:8765/mcp. Register that URL in Agent HQ's Tools tab, tick
"Controls an agent desktop", and tick it on the agent that owns this desktop.

For web pages, prefer the Playwright MCP server on :8931 (text, no vision model).
Use this server for things that are only visible as pixels.

Screenshots need a vision-capable model on the agent using them.

Agent HQ also uses three hidden ahq_* tools here to move chat attachments onto the desktop
(into ~/Uploads, for uploading to websites) and files the agent downloaded back to the chat.
Agents never see them: they use attachment_to_desktop / list_desktop_files / send_from_desktop.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.utilities.types import Image
from PIL import ImageGrab

DISPLAY = os.environ.get("DISPLAY", ":1")
MAX_WIDTH = int(os.environ.get("SCREENSHOT_MAX_WIDTH", "1280"))

mcp = FastMCP("desktop", instructions=(
    "Controls a Linux XFCE desktop. Take a screenshot first, act with click/type/key, "
    "then screenshot again to confirm. Coordinates are in screenshot pixels."))

_scale = 1.0  # screen pixels per screenshot pixel, updated by each screenshot

# Files: everything under the desktop user's home, plus the browser tool's output folder
# (where pages the Playwright MCP server downloads end up).
HOME = Path(os.environ.get("DESKTOP_FILES_HOME") or os.environ.get("HOME") or "/config").resolve()
BROWSER_OUT = Path(os.environ.get("PLAYWRIGHT_OUTPUT_DIR", "/tmp/playwright-mcp")).resolve()
UPLOADS = HOME / "Uploads"
MAX_TRANSFER = int(os.environ.get("DESKTOP_MAX_TRANSFER", str(25 * 1024 * 1024)))
ALLOW = {"agent_hq/approval": "allow"}


def _xdo(*args: str) -> str:
    env = {**os.environ, "DISPLAY": DISPLAY}
    out = subprocess.run(["xdotool", *args], env=env, capture_output=True, text=True, timeout=20)
    if out.returncode != 0:
        raise RuntimeError(f"xdotool {' '.join(map(shlex.quote, args))}: {out.stderr.strip()}")
    return out.stdout.strip()


def _pt(x: int, y: int) -> tuple[str, str]:
    return str(round(x * _scale)), str(round(y * _scale))


@mcp.tool(annotations={"readOnlyHint": True})
def screenshot() -> Image:
    """Capture the whole screen. Returns a PNG, scaled down to at most 1280px wide."""
    global _scale
    img = ImageGrab.grab(xdisplay=DISPLAY)
    _scale = 1.0
    if img.width > MAX_WIDTH:
        _scale = img.width / MAX_WIDTH
        img = img.resize((MAX_WIDTH, round(img.height / _scale)))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Image(data=buf.getvalue(), format="png")


@mcp.tool
def click(x: int, y: int, button: str = "left", double: bool = False) -> str:
    """Click at screenshot coordinates. button: left, middle or right."""
    b = {"left": "1", "middle": "2", "right": "3"}[button]
    _xdo("mousemove", *_pt(x, y), "click", *(["--repeat", "2"] if double else []), b)
    return f"clicked {button} at {x},{y}"


@mcp.tool
def type_text(text: str) -> str:
    """Type text at the current focus."""
    _xdo("type", "--delay", "20", "--", text)
    return f"typed {len(text)} characters"


@mcp.tool
def key(combo: str) -> str:
    """Press a key or combination, e.g. 'Return', 'ctrl+l', 'alt+F4'."""
    _xdo("key", "--", combo)
    return f"pressed {combo}"


@mcp.tool
def scroll(x: int, y: int, direction: str = "down", amount: int = 3) -> str:
    """Scroll at a point. direction: up or down."""
    b = "4" if direction == "up" else "5"
    _xdo("mousemove", *_pt(x, y), "click", "--repeat", str(amount), b)
    return f"scrolled {direction} {amount}"


@mcp.tool
def open_url(url: str) -> str:
    """Open a URL in the desktop's browser (the same Chromium the browser_* tools drive)."""
    env = {**os.environ, "DISPLAY": DISPLAY}
    cmd = ["agent-browser", url] if shutil.which("agent-browser") else ["xdg-open", url]
    subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return f"opening {url}; take a screenshot in a few seconds to see it"


# --------------------------------------------------------------------------- files (Agent HQ)


def _resolve(path: str) -> Path:
    """A path on the desktop: ~/x, relative to home, or absolute under home or the browser's
    download folder. Anything else is refused."""
    p = (path or "").strip()
    if p.startswith("~"):
        p = str(HOME) + p[1:]
    full = (Path(p) if os.path.isabs(p) else HOME / p).resolve()
    for root in (HOME, BROWSER_OUT):
        if full == root or root in full.parents:
            return full
    raise ValueError(f"{path} is outside the desktop's home folder")


def _free_name(folder: Path, name: str) -> Path:
    name = re.sub(r"[\x00-\x1f/\\]", "_", Path(name or "file").name) or "file"
    target, stem, suffix, n = folder / name, Path(name).stem, Path(name).suffix, 1
    while target.exists():
        target, n = folder / f"{stem} ({n}){suffix}", n + 1
    return target


@mcp.tool(meta=ALLOW)
def ahq_put_file(name: str, data_b64: str) -> str:
    """(Agent HQ) Save a file into ~/Uploads (a new name if one is taken)."""
    data = base64.b64decode(data_b64)
    if len(data) > MAX_TRANSFER:
        raise ValueError(f"file is larger than {MAX_TRANSFER} bytes")
    UPLOADS.mkdir(parents=True, exist_ok=True)
    target = _free_name(UPLOADS, name)
    target.write_bytes(data)
    return json.dumps({"path": str(target), "bytes": len(data)})


@mcp.tool(meta=ALLOW, annotations={"readOnlyHint": True})
def ahq_get_file(path: str, max_bytes: int = MAX_TRANSFER) -> str:
    """(Agent HQ) Read a file from the desktop as base64."""
    try:
        p = _resolve(path)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    if not p.is_file():
        return json.dumps({"error": f"no file {path} on the desktop"})
    size, cap = p.stat().st_size, min(max_bytes, MAX_TRANSFER)
    if size > cap:
        return json.dumps({"error": f"{path} is {size} bytes, more than the {cap} allowed"})
    return json.dumps({"name": p.name, "bytes": size, "data_b64": base64.b64encode(p.read_bytes()).decode()})


@mcp.tool(meta=ALLOW, annotations={"readOnlyHint": True})
def ahq_list_files(folder: str = "", limit: int = 30) -> str:
    """(Agent HQ) Newest files in a folder, or in ~/Downloads and the browser's download folder."""
    roots = [_resolve(folder)] if folder.strip() else [HOME / "Downloads", BROWSER_OUT]
    found = []
    for root in roots:
        if not root.is_dir():
            continue
        for f in root.rglob("*"):
            if f.is_file() and not f.name.startswith(".") and not f.name.endswith((".crdownload", ".part")):
                st = f.stat()
                found.append((st.st_mtime, f, st.st_size))
    found.sort(key=lambda x: x[0], reverse=True)
    home = str(HOME)
    return json.dumps([{"path": ("~" + str(f)[len(home):]) if str(f).startswith(home) else str(f),
                        "bytes": size, "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(m))}
                       for m, f, size in found[:max(1, min(limit, 200))]])


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("DESKTOP_MCP_PORT", "8765")))
