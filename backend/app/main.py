"""API process: REST + WebSocket + web UI, MQTT bridge, optional embedded worker.

    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .api import approvals as approvals_api
from .api import skills as skills_api
from .api import learning as learning_api
from .api import profile as profile_api
from .api import auth as auth_api
from .api import agents, avatars, desktop, memory as memory_api, providers, schedules, subagents as subagents_api, subscriptions, system, threads, tool_servers
from .broker import Broker
from .config import settings
from .db import init_db
from .runtime import Runtime, open_checkpointer
from .telemetry import setup_telemetry

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("agent_hq")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_telemetry()
    init_db()
    from .auth import ensure_setup_code

    ensure_setup_code()  # with no account yet, log a one-time code for creating yours
    broker = Broker(settings.redis_url)
    app.state.broker = broker
    async with contextlib.AsyncExitStack() as stack:
        saver = await stack.enter_async_context(open_checkpointer()) if settings.embed_worker else None
        rt = Runtime(broker, saver)
        app.state.runtime = rt
        background: list[asyncio.Task] = []

        if settings.embed_worker:
            from .worker import Worker

            worker = Worker(rt, name=f"{settings.worker_name}-embedded", pools=settings.worker_pools,
                            concurrency=settings.worker_concurrency)
            await worker.start()
            app.state.worker = worker
            stack.push_async_callback(worker.stop)

        background.append(asyncio.create_task(memory_api.nightly_tidy_loop(broker)))
        from .learning import loop as learning_loop

        background.append(asyncio.create_task(learning_loop(rt)))
        if settings.scheduler_enabled:
            from .scheduler import scheduler_loop

            background.append(asyncio.create_task(scheduler_loop(rt)))

        if settings.mqtt_url:
            from .mqtt_bridge import MqttBridge

            app.state.mqtt = MqttBridge(rt, settings.mqtt_url)
            background.append(asyncio.create_task(app.state.mqtt.run()))
        else:
            log.info("AHQ_MQTT_URL not set; MQTT bridge disabled")

        try:
            yield
        finally:
            for t in background:
                t.cancel()
            await asyncio.gather(*background, return_exceptions=True)
    await broker.close()


def _fastapi_kwargs() -> dict:
    """Newer FastAPI versions trace every HTTP request by themselves. The UI polls several
    endpoints, which would bury the agent traces in Phoenix, so turn that off; agent work
    is traced per job instead."""
    import inspect

    if "telemetry" in inspect.signature(FastAPI.__init__).parameters:
        return {"telemetry": {"tracing": False, "metrics": False, "logs": False}}
    return {}


def create_app(lifespan_=lifespan) -> FastAPI:
    app = FastAPI(title="Agent HQ", lifespan=lifespan_, **_fastapi_kwargs())
    # Sign-in is required for /api and /ws (see app/auth.py). Added before CORS so CORS
    # wraps it and even 401s carry CORS headers for the dev server.
    app.add_middleware(auth_api.AuthMiddleware)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                       allow_methods=["*"], allow_headers=["*"], allow_credentials=True)
    for r in (auth_api.router, approvals_api.router, skills_api.router, learning_api.router, profile_api.router, agents.router, avatars.router, desktop.router, memory_api.router, providers.router, schedules.router, subagents_api.router, tool_servers.router, threads.router, subscriptions.router,
              system.router):
        app.include_router(r)

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/{path:path}", include_in_schema=False)
    def api_not_found(path: str):
        raise HTTPException(404, "No such API endpoint")

    # Serve the built Angular app (copied into backend/static by the Dockerfile).
    if STATIC_DIR.is_dir():
        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            f = STATIC_DIR / path
            return FileResponse(f if path and f.is_file() else STATIC_DIR / "index.html")

    return app


app = create_app()
