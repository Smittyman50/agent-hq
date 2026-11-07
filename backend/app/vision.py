"""Vision as a tool: a separate (usually small, local) vision model looks at the desktop
on the agent's behalf, so the agent's own model never has screenshots in its context.

Tools given to an agent that has a vision model and a desktop:
  look(question)                 -> text answer about what is on screen
  locate(description)            -> {found, x, y} in desktop click coordinates
  click_on(description, double)  -> locate, then click there

Accuracy helpers:
  * zoom: after a first guess, the area around it is cropped, enlarged and asked again.
  * coordinate formats: "relative_1000" (0-1000 scale, Qwen-VL family, Holo) or
    "pixels" (absolute pixels of the image the model was given, e.g. UI-TARS).
"""

from __future__ import annotations

import base64
import io
import re
from dataclasses import dataclass

from PIL import Image
from pydantic_ai import Agent, BinaryContent

from .llm import build_model_for
from .models import AgentDef, ToolServer

_has_screenshot: dict[str, bool] = {}


async def screenshot_server(agent: AgentDef, servers: list[ToolServer]) -> ToolServer | None:
    """The agent's desktop server that offers a `screenshot` tool (checked once, cached)."""
    from .runtime import mcp_toolset

    for srv in servers:
        if srv.id not in _has_screenshot:
            try:
                ts = mcp_toolset(srv)
                async with ts:
                    names = {t.name for t in await ts.client.list_tools()}
                _has_screenshot[srv.id] = "screenshot" in names
            except Exception:
                continue  # unreachable now; try again next time
        if _has_screenshot[srv.id]:
            return srv
    return None

LOOK_PROMPT = ("You are looking at a screenshot of a Linux desktop for an AI agent that cannot "
               "see it. Answer the question precisely and concisely, quoting any text or numbers "
               "exactly as shown. If the answer is not visible, say so.\n\nQuestion: {q}")

LOCATE_PROMPT = {
    "relative_1000": ("Find this element in the screenshot: {d}\n"
                      "Reply with only its center as JSON: {{\"x\": X, \"y\": Y}}, where X and Y are "
                      "on a 0-1000 scale relative to the image width and height. "
                      "If it is not visible, reply {{\"found\": false}}."),
    "pixels": ("Find this element in the screenshot: {d}\n"
               "The image is {w}x{h} pixels. Reply with only its center as JSON: "
               "{{\"x\": X, \"y\": Y}} in pixels. If it is not visible, reply {{\"found\": false}}."),
}

_NUM = re.compile(r"(?<![A-Za-z_\d.])-?\d+(?:\.\d+)?")  # ignore digits in keys like point_2d


def parse_point(text: str, mode: str, width: int, height: int) -> tuple[float, float] | None:
    """Pull a point out of a model reply and convert it to image pixels."""
    if re.search(r"found\W*:\W*false", text, re.I) or re.search(r"\bnot (visible|found)\b", text, re.I):
        return None
    nums = [float(n) for n in _NUM.findall(text)]
    if len(nums) < 2:
        return None
    if len(nums) >= 4 and ("box" in text.lower() or len(nums) == 4):
        x, y = (nums[0] + nums[2]) / 2, (nums[1] + nums[3]) / 2  # a box: use its center
    else:
        x, y = nums[0], nums[1]
    if mode == "relative_1000":
        x, y = x * width / 1000, y * height / 1000
    if not (0 <= x <= width and 0 <= y <= height):
        return None
    return x, y


def png_size(png: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(png)) as im:
        return im.size


def crop_zoom(png: bytes, cx: float, cy: float, frac: float = 0.35, min_side: int = 320,
              upscale_to: int = 1024) -> tuple[bytes, tuple[float, float, float]]:
    """Crop around (cx, cy) and enlarge. Returns (png, (left, top, scale)) where
    original = left + crop_xy / scale."""
    with Image.open(io.BytesIO(png)) as im:
        w, h = im.size
        cw = max(min_side, int(w * frac))
        ch = max(min_side, int(h * frac))
        cw, ch = min(cw, w), min(ch, h)
        left = int(min(max(cx - cw / 2, 0), w - cw))
        top = int(min(max(cy - ch / 2, 0), h - ch))
        crop = im.crop((left, top, left + cw, top + ch))
        scale = max(1.0, upscale_to / max(cw, ch))
        if scale > 1:
            crop = crop.resize((round(cw * scale), round(ch * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        crop.convert("RGB").save(buf, format="PNG")
        return buf.getvalue(), (left, top, scale)


@dataclass
class DesktopEyes:
    """Screenshots and clicks via the desktop MCP server, judgement via the vision model."""

    agent: AgentDef
    server: ToolServer
    thread_id: str | None = None  # traces group vision calls with the conversation

    def _vision_agent(self) -> Agent:
        model = build_model_for(self.agent.vision_provider_id, self.agent.vision_model,
                                f"{self.agent.name} (vision)")
        return Agent(model, name=f"{self.agent.name} vision", output_type=str)

    async def _call(self, tool: str, args: dict):
        from .runtime import mcp_toolset

        ts = mcp_toolset(self.server)
        async with ts:
            return await ts.client.call_tool(tool, args)

    async def screenshot(self) -> bytes:
        result = await self._call("screenshot", {})
        for part in result.content:
            data = getattr(part, "data", None)
            if data:
                return base64.b64decode(data) if isinstance(data, str) else data
        raise RuntimeError("the desktop returned no image")

    async def ask(self, prompt: str, png: bytes) -> str:
        r = await self._vision_agent().run([prompt, BinaryContent(data=png, media_type="image/png")],
                                           conversation_id=self.thread_id)
        return str(r.output).strip()

    async def look(self, question: str) -> str:
        return await self.ask(LOOK_PROMPT.format(q=question), await self.screenshot())

    async def locate(self, description: str) -> dict:
        png = await self.screenshot()
        w, h = png_size(png)
        mode = self.agent.vision_coords if self.agent.vision_coords in LOCATE_PROMPT else "relative_1000"
        reply = await self.ask(LOCATE_PROMPT[mode].format(d=description, w=w, h=h), png)
        point = parse_point(reply, mode, w, h)
        if point is None:
            return {"found": False, "description": description}
        x, y = point
        refined = False
        if self.agent.vision_zoom:
            crop, (left, top, scale) = crop_zoom(png, x, y)
            cw, ch = png_size(crop)
            reply2 = await self.ask(LOCATE_PROMPT[mode].format(d=description, w=cw, h=ch), crop)
            p2 = parse_point(reply2, mode, cw, ch)
            if p2 is not None:
                x, y = left + p2[0] / scale, top + p2[1] / scale
                refined = True
        return {"found": True, "x": round(x), "y": round(y), "refined": refined,
                "screen": {"width": w, "height": h}}

    async def click(self, x: int, y: int, double: bool) -> str:
        result = await self._call("click", {"x": x, "y": y, "double": double})
        return " ".join(getattr(p, "text", "") for p in result.content).strip()
