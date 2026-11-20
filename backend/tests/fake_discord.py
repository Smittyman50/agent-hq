"""A fake Discord (REST API and Gateway) for the tests: enough to drive the Discord channel
end to end. The Gateway says hello, checks IDENTIFY's token and intents, sends READY,
answers heartbeats and delivers the events a test pushes. It runs in a thread (uvicorn)."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from typing import Any

import uvicorn
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse, Response

GOOD_TOKEN = "discord-good-token"
BOT = {"id": "5550001", "username": "agenthq", "bot": True}
APP_ID = "5550001"
HUMAN = {"id": "4242", "username": "mike", "global_name": "Mike"}


class FakeDiscord:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls: list[tuple[str, str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.identified: list[dict[str, Any]] = []
        self.resumed = 0
        self.connections = 0
        self.next_id = 900000
        self.seq = 0
        self.drop_now = False                 # close the open connection (tests resuming)
        self.content_allowed = False          # Message Content enabled in the "Developer Portal"
        self.app = FastAPI()
        self.app.api_route("/api/v10/{path:path}", methods=["GET", "POST", "PATCH", "PUT", "DELETE"])(self._rest)
        self.app.websocket("/gateway/")(self._gateway)
        self.port = 0
        self.server: uvicorn.Server | None = None

    @property
    def api(self) -> str:
        return f"http://127.0.0.1:{self.port}/api/v10"

    def new_id(self) -> str:
        with self.lock:
            self.next_id += 1
            return str(self.next_id)

    # -- what tests look at
    def posted(self, method: str = "POST", prefix: str = "/channels/") -> list[tuple[str, Any]]:
        with self.lock:
            return [(p, b) for m, p, b in self.calls if m == method and p.startswith(prefix)]

    def messages(self) -> list[dict[str, Any]]:
        return [b | {"_path": p} for p, b in self.posted() if p.endswith("/messages")]

    async def wait(self, check, timeout: float = 15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            got = check()
            if got:
                return got
            await asyncio.sleep(0.05)
        raise AssertionError(f"condition never held; calls: {self.calls[-8:]}")

    # -- what tests make people do
    def push(self, kind: str, data: dict[str, Any]) -> None:
        with self.lock:
            self.events.append({"t": kind, "d": data})

    def dm(self, text: str, *, user: dict | None = None, channel: str = "dm-4242",
           reply_to: str | None = None, mention: bool = False, guild: str | None = None,
           reply_author: dict | None = None) -> dict[str, Any]:
        user = user or HUMAN
        msg: dict[str, Any] = {"id": self.new_id(), "channel_id": channel, "author": user,
                               "content": text, "mentions": [BOT] if mention else [], "attachments": []}
        if guild:
            msg["guild_id"] = guild
            if not (mention or self.content_allowed or reply_author):
                msg["content"] = ""  # what Discord does without the Message Content intent
        if reply_to:
            msg["message_reference"] = {"message_id": reply_to, "channel_id": channel}
            msg["referenced_message"] = {"id": reply_to, "author": reply_author or BOT}
        self.push("MESSAGE_CREATE", msg)
        return msg

    def press(self, message_id: str, custom_id: str, *, user: dict | None = None,
              channel: str = "dm-4242", guild: str | None = None) -> str:
        iid = self.new_id()
        d: dict[str, Any] = {"id": iid, "token": f"tok{iid}", "type": 3, "application_id": APP_ID,
                             "channel_id": channel, "message": {"id": message_id},
                             "data": {"custom_id": custom_id, "component_type": 2}}
        if guild:
            d["guild_id"], d["member"] = guild, {"user": user or HUMAN}
        else:
            d["user"] = user or HUMAN
        self.push("INTERACTION_CREATE", d)
        return iid

    # -- REST
    async def _rest(self, path: str, request: Request):
        path = "/" + path
        try:
            body = await request.json()
        except Exception:
            body = None
        public = path.startswith("/interactions/") or path.startswith("/webhooks/")
        if not public and request.headers.get("authorization") != f"Bot {GOOD_TOKEN}":
            return JSONResponse({"message": "401: Unauthorized", "code": 0}, status_code=401)
        with self.lock:
            self.calls.append((request.method, path, body))
        if path == "/users/@me":
            return BOT
        if path == "/oauth2/applications/@me":
            return {"id": APP_ID, "name": "Agent HQ"}
        if path == "/gateway/bot":
            return {"url": f"ws://127.0.0.1:{self.port}/gateway", "shards": 1}
        if request.method == "POST" and path.endswith("/messages"):
            return {"id": self.new_id(), "channel_id": path.split("/")[2], **(body or {})}
        if request.method == "PUT" or path.startswith("/interactions/"):
            return Response(status_code=204)
        return {"id": path.rsplit("/", 1)[-1], **(body or {})}

    # -- Gateway
    async def _gateway(self, ws: WebSocket):
        await ws.accept()
        with self.lock:
            self.connections += 1
        await ws.send_text(json.dumps({"op": 10, "d": {"heartbeat_interval": 500}}))
        first = json.loads(await ws.receive_text())
        if first.get("op") == 2:
            d = first["d"]
            if d.get("token") != GOOD_TOKEN:
                await ws.close(code=4004)
                return
            if d.get("intents", 0) & (1 << 15) and not self.content_allowed:
                await ws.close(code=4014)
                return
            with self.lock:
                self.identified.append(d)
            await self._send(ws, "READY", {"session_id": "sess1", "user": BOT,
                                           "resume_gateway_url": f"ws://127.0.0.1:{self.port}/gateway"})
        elif first.get("op") == 6:
            with self.lock:
                self.resumed += 1
            await self._send(ws, "RESUMED", {})

        async def reader():
            while True:
                msg = json.loads(await ws.receive_text())
                if msg.get("op") == 1:
                    await ws.send_text(json.dumps({"op": 11}))

        task = asyncio.create_task(reader())
        try:
            while not task.done():
                if self.drop_now:
                    self.drop_now = False
                    await ws.close(code=4000)
                    return
                with self.lock:
                    batch, self.events = self.events, []
                for e in batch:
                    await self._send(ws, e["t"], e["d"])
                await asyncio.sleep(0.02)
        finally:
            task.cancel()

    async def _send(self, ws: WebSocket, kind: str, data: dict[str, Any]) -> None:
        with self.lock:
            self.seq += 1
            seq = self.seq
        await ws.send_text(json.dumps({"op": 0, "t": kind, "s": seq, "d": data}))

    # -- lifecycle
    def start(self) -> None:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port,
                                                    log_level="warning", lifespan="off", ws="websockets-sansio"))
        threading.Thread(target=self.server.run, daemon=True).start()
        for _ in range(200):
            if self.server.started:
                return
            time.sleep(0.02)
        raise RuntimeError("fake Discord didn't start")

    def stop(self) -> None:
        if self.server:
            self.server.should_exit = True
