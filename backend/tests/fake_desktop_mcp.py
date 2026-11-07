"""Stand-in desktop MCP server: a 1280x720 'screen' with a red Sign in button at
x 900-1000, y 600-640. Clicks are appended to the file named by CLICK_LOG."""

import io
import os

from fastmcp import FastMCP
from fastmcp.utilities.types import Image
from PIL import Image as PILImage, ImageDraw

mcp = FastMCP("fake-desktop")


@mcp.tool
def screenshot() -> Image:
    """Capture the whole screen."""
    im = PILImage.new("RGB", (1280, 720), (230, 230, 230))
    d = ImageDraw.Draw(im)
    d.rectangle((900, 600, 1000, 640), fill=(220, 30, 30))
    d.text((920, 612), "Sign in", fill=(255, 255, 255))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return Image(data=buf.getvalue(), format="png")


@mcp.tool
def click(x: int, y: int, button: str = "left", double: bool = False) -> str:
    """Click at screenshot coordinates."""
    with open(os.environ["CLICK_LOG"], "a") as f:
        f.write(f"{x},{y},{double}\n")
    return f"clicked {button} at {x},{y}"


@mcp.tool
def type_text(text: str) -> str:
    """Type text."""
    return f"typed {len(text)} characters"


if __name__ == "__main__":
    mcp.run()
