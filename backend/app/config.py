"""Runtime settings, read from environment variables (prefix AHQ_)."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(f"AHQ_{name}", default)


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "./data")))
    # Database. Use Postgres whenever workers run on more than one machine:
    #   AHQ_DB_URL=postgresql://agent_hq:secret@db-host:5432/agent_hq
    db_url_raw: str = field(default_factory=lambda: _env("DB_URL", ""))
    # Valkey or Redis: job streams, results, locks, live UI events, worker registry.
    redis_url: str = field(default_factory=lambda: _env("REDIS_URL", "redis://localhost:6379/0"))
    # MQTT broker for incoming events, e.g. mqtt://agent-hq:secret@broker:1883 (mqtts:// for TLS).
    # Leave empty to disable the MQTT bridge.
    mqtt_url: str = field(default_factory=lambda: _env("MQTT_URL", ""))
    mqtt_max_payload: int = field(default_factory=lambda: int(_env("MQTT_MAX_PAYLOAD", "4000")))
    # Run scheduled agent tasks from this API process (on by default; safe with several API
    # processes, since each run is claimed with a lock).
    scheduler_enabled: bool = field(default_factory=lambda: _env("SCHEDULER", "1").lower() not in ("0", "false", "no"))
    # How deep a delegation chain may go (A -> B -> C ...) before it is refused.
    max_delegation_depth: int = field(default_factory=lambda: int(_env("MAX_DELEGATION_DEPTH", "4")))
    # A waiting delegation switches to background mode after this many seconds.
    delegate_wait_timeout: float = field(default_factory=lambda: float(_env("DELEGATE_WAIT_TIMEOUT", "1800")))
    # Largest avatar upload accepted, in bytes.
    avatar_max_bytes: int = field(default_factory=lambda: int(_env("AVATAR_MAX_BYTES", str(3 * 1024 * 1024))))
    # How long an agent waits for you after asking you to take over its desktop.
    handoff_timeout: float = field(default_factory=lambda: float(_env("HANDOFF_TIMEOUT", "1800")))
    # Worker reliability knobs.
    worker_heartbeat: float = field(default_factory=lambda: float(_env("WORKER_HEARTBEAT", "15")))
    claim_idle_ms: int = field(default_factory=lambda: int(_env("CLAIM_IDLE_MS", "60000")))
    max_attempts: int = field(default_factory=lambda: int(_env("MAX_ATTEMPTS", "3")))
    # Worker identity and pools (used by `python -m app.worker` and by the embedded worker).
    worker_name: str = field(default_factory=lambda: _env("WORKER_NAME", socket.gethostname()))
    worker_pools: list[str] = field(
        default_factory=lambda: [p.strip() for p in _env("WORKER_POOLS", "default").split(",") if p.strip()])
    worker_concurrency: int = field(default_factory=lambda: int(_env("WORKER_CONCURRENCY", "4")))
    # Run a worker inside the API process too (handy on a single machine).
    embed_worker: bool = field(default_factory=lambda: _env("EMBED_WORKER", "0") in ("1", "true", "yes"))
    # Allowed browser origins for the Angular dev server.
    cors_origins: list[str] = field(
        default_factory=lambda: _env("CORS_ORIGINS", "http://localhost:4200").split(","))

    @property
    def is_postgres(self) -> bool:
        return self.db_url_raw.startswith(("postgres://", "postgresql"))

    @property
    def db_url(self) -> str:
        """SQLAlchemy URL."""
        if not self.db_url_raw:
            return f"sqlite:///{self.data_dir / 'agent_hq.db'}"
        if self.is_postgres:
            rest = self.db_url_raw.split("://", 1)[1]
            return f"postgresql+psycopg://{rest}"
        return self.db_url_raw

    @property
    def checkpoint_url(self) -> str:
        """LangGraph checkpointer: same Postgres database, or a local SQLite file."""
        if self.is_postgres:
            rest = self.db_url_raw.split("://", 1)[1]
            return f"postgresql://{rest}"
        return str(self.data_dir / "checkpoints.db")


settings = Settings()


def reload_settings() -> Settings:
    """Re-read the environment (used by tests)."""
    global settings
    new = Settings()
    settings.__dict__.update(new.__dict__)
    return settings
