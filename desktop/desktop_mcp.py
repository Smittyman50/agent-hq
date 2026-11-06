"""MCP server that lets an agent see and drive the Linux desktop it runs inside.

Runs inside each agent's desktop container (see Dockerfile) and listens on
http://0.0.0.0:8765/mcp. Register that URL in Agent HQ's Tools tab and tick it
on the agent that owns this desktop.

Screenshots need a vision-capable model on the agent using them.
"""

from __future__ import annotations

import io
import os
import shlex
import subprocess

from fastmcp import FastMCP
from fastmcp.utilities.types import Image
from PIL import ImageGrab

DISPLAY = os.environ.get("DISPLAY", ":1")
MAX_WIDTH = int(os.environ.get("SCREENSHOT_MAX_WIDTH", "1280"))

mcp = FastMCP("desktop", instructions=(
    "Controls a Linux XFCE desktop. Take a screenshot first, act with click/type/key, "
    "then screenshot again to confirm. Coordinates are in screenshot pixels."))

_scale = 1.0  # screen pixels per screenshot pixel, updated by each screenshot


def _xdo(*args: str) -> str:
    env = {**os.environ, "DISPLAY": DISPLAY}
    out = subprocess.run(["xdotool", *args], env=env, capture_output=True, text=True, timeout=20)
    if out.returncode != 0:
        raise RuntimeError(f"xdotool {' '.join(map(shlex.quote, args))}: {out.stderr.strip()}")
    return out.stdout.strip()


def _pt(x: int, y: int) -> tuple[str, str]:
    return str(round(x * _scale)), str(round(y * _scale))


@mcp.tool
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
    """Open a URL in the desktop's browser."""
    env = {**os.environ, "DISPLAY": DISPLAY}
    subprocess.Popen(["xdg-open", url], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return f"opening {url}; take a screenshot in a few seconds to see it"


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("DESKTOP_MCP_PORT", "8765")))
