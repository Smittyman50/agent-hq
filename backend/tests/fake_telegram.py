"""A fake Telegram Bot API server for the tests: enough of sendMessage, editMessageText,
getUpdates, answerCallbackQuery, setMessageReaction, getMe and deleteWebhook to drive
the Telegram channel end to end. It runs in a thread (uvicorn), on a free port."""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

import uvicorn
from fastapi import FastAPI, Request

GOOD_TOKEN = "123456:good-token"
BOT = {"id": 999, "is_bot": True, "first_name": "HQ Bot", "username": "hq_test_bot"}


class FakeTelegram:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.updates: list[dict[str, Any]] = []
        self.next_update = 1
        self.next_message = 100
        self.polls = 0
        self.app = FastAPI()
        self.app.post("/bot{token}/{method}")(self._handle)
        self.port = 0
        self.server: uvicorn.Server | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # -- what tests look at
    def sent(self, method: str = "sendMessage") -> list[dict[str, Any]]:
        with self.lock:
            return [p for m, p in self.calls if m == method]

    async def wait_sent(self, n: int, method: str = "sendMessage", timeout: float = 15) -> list[dict[str, Any]]:
        import asyncio

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            got = self.sent(method)
            if len(got) >= n:
                return got
            await asyncio.sleep(0.05)
        raise AssertionError(f"expected {n} {method}, got {self.sent(method)}")

    # -- what tests make the user do
    def push(self, update: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            update = {"update_id": self.next_update, **update}
            self.next_update += 1
            self.updates.append(update)
        return update

    def user_message(self, text: str, *, user_id: int = 42, chat_id: int | None = None,
                     reply_to: str | int | None = None, name: str = "Mike") -> dict[str, Any]:
        with self.lock:
            mid = self.next_message
            self.next_message += 1
        msg: dict[str, Any] = {"message_id": mid, "date": int(time.time()), "text": text,
                               "from": {"id": user_id, "is_bot": False, "first_name": name},
                               "chat": {"id": chat_id or user_id, "type": "private"}}
        if reply_to is not None:
            msg["reply_to_message"] = {"message_id": int(reply_to), "chat": msg["chat"]}
        return {"message": msg}

    def tap(self, message: dict[str, Any], data: str, *, user_id: int = 42, name: str = "Mike") -> dict[str, Any]:
        return {"callback_query": {"id": f"cq{self.next_update}", "data": data,
                                   "from": {"id": user_id, "is_bot": False, "first_name": name},
                                   "message": {"message_id": int(message["message_id"]),
                                               "chat": {"id": int(message["chat_id"])}}}}

    # -- the API
    async def _handle(self, token: str, method: str, request: Request):
        try:
            params = await request.json()
        except Exception:
            params = {}
        if token != GOOD_TOKEN:
            return {"ok": False, "error_code": 401, "description": "Unauthorized"}
        if method == "getUpdates":
            return {"ok": True, "result": await self._updates(params)}
        with self.lock:
            self.calls.append((method, params))
            if method == "getMe":
                return {"ok": True, "result": BOT}
            if method == "sendMessage":
                mid = self.next_message
                self.next_message += 1
                params["message_id"] = mid
                return {"ok": True, "result": {"message_id": mid, "chat": {"id": params.get("chat_id")},
                                               "text": params.get("text")}}
        return {"ok": True, "result": True}

    async def _updates(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        import asyncio

        offset = int(params.get("offset") or 0)
        deadline = time.monotonic() + min(float(params.get("timeout") or 0), 0.5)
        while True:
            with self.lock:
                self.polls += 1
                self.updates = [u for u in self.updates if u["update_id"] >= offset]
                if self.updates or time.monotonic() >= deadline:
                    return list(self.updates)
            await asyncio.sleep(0.02)

    # -- lifecycle
    def start(self) -> None:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port,
                                                    log_level="warning", lifespan="off"))
        threading.Thread(target=self.server.run, daemon=True).start()
        for _ in range(200):
            if self.server.started:
                return
            time.sleep(0.02)
        raise RuntimeError("fake Telegram didn't start")

    def stop(self) -> None:
        if self.server:
            self.server.should_exit = True
