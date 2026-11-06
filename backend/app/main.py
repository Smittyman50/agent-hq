from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from .api import agents, threads, tool_servers
from .config import settings
from .db import init_db
from .runtime import Runtime
from .telemetry import setup_telemetry

logging.basicConfig(level=logging.INFO)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_telemetry()
    init_db()
    async with AsyncSqliteSaver.from_conn_string(settings.checkpoint_path) as saver:
        app.state.runtime = Runtime(saver)
        await app.state.runtime.recover_interrupted_jobs()
        yield


def create_app(lifespan_=lifespan) -> FastAPI:
    app = FastAPI(title="Agent HQ", lifespan=lifespan_)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                       allow_methods=["*"], allow_headers=["*"])
    app.include_router(agents.router)
    app.include_router(tool_servers.router)
    app.include_router(threads.router)

    @app.get("/api/health")
    def health():
        return {"ok": True}

    # Serve the built Angular app (copied into backend/static by the Dockerfile).
    if STATIC_DIR.is_dir():
        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            f = STATIC_DIR / path
            return FileResponse(f if path and f.is_file() else STATIC_DIR / "index.html")

    return app


app = create_app()
