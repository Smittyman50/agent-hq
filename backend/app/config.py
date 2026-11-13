"""Runtime settings, read from environment variables (prefix AHQ_)."""

from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(f"AHQ_{name}", default)


HOST_HOSTNAME_FILE = Path("/etc/host_hostname")  # the Docker host's /etc/hostname, mounted read-only
_CONTAINER_ID = re.compile(r"^[0-9a-f]{12}$")


def _in_container(hostname: str) -> bool:
    return Path("/.dockerenv").exists() or bool(_CONTAINER_ID.match(hostname))


def _host_name() -> str:
    """The machine's name: AHQ_HOST_NAME, else the Docker host's mounted /etc/hostname."""
    name = _env("HOST_NAME", "").strip()
    if not name:
        try:
            if HOST_HOSTNAME_FILE.is_file():
                name = HOST_HOSTNAME_FILE.read_text().strip()
        except OSError:
            name = ""
    return name.split(".")[0]  # shockwave.home.arpa -> shockwave


def default_worker_name() -> str:
    """A readable name that stays unique when a service is scaled.

    AHQ_WORKER_NAME wins if set. Outside a container the machine's hostname is used, as
    before. In a container the hostname is the container ID, so the name becomes
    <AHQ_WORKER_PREFIX>-<host>-<last 4 of the container ID>, e.g. worker-shockwave-7090:
    which service, which machine, and which replica.
    """
    explicit = _env("WORKER_NAME", "").strip()
    if explicit:
        return explicit
    hostname = socket.gethostname()
    if not _in_container(hostname):
        return hostname
    parts = [_env("WORKER_PREFIX", "worker").strip(), _host_name(), hostname[-4:]]
    return "-".join(p for p in parts if p)


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
    # Where Agent HQ publishes notifications for you (sub-agent requests and decisions),
    # on the same broker as AHQ_MQTT_URL. JSON payloads; see the README.
    mqtt_notify_topic: str = field(default_factory=lambda: _env("MQTT_NOTIFY_TOPIC", "agent-hq/notifications"))
    # Optional address of the web UI, e.g. http://shockwave:8000, used for links in notifications.
    public_url: str = field(default_factory=lambda: _env("PUBLIC_URL", "").rstrip("/"))
    mqtt_max_payload: int = field(default_factory=lambda: int(_env("MQTT_MAX_PAYLOAD", "4000")))
    # Inbound webhooks (POST /hooks/<name>): largest body accepted, and how much of it
    # reaches the agent's prompt (JSON bodies are bigger than MQTT payloads).
    webhook_max_body: int = field(default_factory=lambda: int(_env("WEBHOOK_MAX_BODY", "1048576")))
    webhook_max_payload: int = field(default_factory=lambda: int(_env("WEBHOOK_MAX_PAYLOAD", "8000")))
    # How long a sender's delivery ID is remembered for the duplicate check, and how many
    # deliveries per hook the log keeps.
    webhook_dedupe_seconds: int = field(default_factory=lambda: int(_env("WEBHOOK_DEDUPE_SECONDS", "86400")))
    webhook_keep_deliveries: int = field(default_factory=lambda: int(_env("WEBHOOK_KEEP_DELIVERIES", "200")))
    # ...and for how many days (older ones are removed hourly).
    webhook_keep_days: float = field(default_factory=lambda: float(_env("WEBHOOK_KEEP_DAYS", "7")))
    # Addresses webhooks are accepted from, unless a hook names its own: private and
    # loopback ranges, i.e. your LAN. Comma-separated CIDRs.
    webhook_networks: str = field(default_factory=lambda: _env(
        "WEBHOOK_NETWORKS", "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.0/8,::1/128,fc00::/7,fe80::/10"))
    # Requests per minute a new hook accepts (0 = no limit); each hook can change it.
    webhook_rate_per_minute: int = field(default_factory=lambda: int(_env("WEBHOOK_RATE_PER_MINUTE", "60")))
    # Encrypts webhook secrets in the database. Any long random string, e.g.
    # `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Keep it safe: hooks
    # stop verifying senders if it changes (rotate their secrets then).
    secret_key: str = field(default_factory=lambda: _env("SECRET_KEY", ""))
    # Run scheduled agent tasks from this API process (on by default; safe with several API
    # processes, since each run is claimed with a lock).
    scheduler_enabled: bool = field(default_factory=lambda: _env("SCHEDULER", "1").lower() not in ("0", "false", "no"))
    # Your time zone (IANA name, e.g. America/New_York). Every agent is told the current
    # date and time in it, so "today", "tomorrow" and "this weekend" mean the right days.
    # Falls back to TZ, then UTC.
    timezone: str = field(default_factory=lambda: _env("TIMEZONE", "").strip()
                          or os.environ.get("TZ", "").strip() or "UTC")
    # How deep a delegation chain may go (A -> B -> C ...) before it is refused.
    max_delegation_depth: int = field(default_factory=lambda: int(_env("MAX_DELEGATION_DEPTH", "4")))
    # A waiting delegation switches to background mode after this many seconds.
    delegate_wait_timeout: float = field(default_factory=lambda: float(_env("DELEGATE_WAIT_TIMEOUT", "1800")))
    # Largest avatar upload accepted, in bytes.
    avatar_max_bytes: int = field(default_factory=lambda: int(_env("AVATAR_MAX_BYTES", str(3 * 1024 * 1024))))
    # Chat attachments: largest file, and most files on one message.
    attachment_max_bytes: int = field(default_factory=lambda: int(_env("ATTACHMENT_MAX_BYTES", str(25 * 1024 * 1024))))
    attachment_max_files: int = field(default_factory=lambda: int(_env("ATTACHMENT_MAX_FILES", "10")))
    # How long an agent waits for you after asking you to take over its desktop.
    handoff_timeout: float = field(default_factory=lambda: float(_env("HANDOFF_TIMEOUT", "1800")))
    # Worker reliability knobs.
    worker_heartbeat: float = field(default_factory=lambda: float(_env("WORKER_HEARTBEAT", "15")))
    claim_idle_ms: int = field(default_factory=lambda: int(_env("CLAIM_IDLE_MS", "60000")))
    max_attempts: int = field(default_factory=lambda: int(_env("MAX_ATTEMPTS", "3")))
    # How often a running job checks whether you pressed Stop, in seconds.
    stop_poll: float = field(default_factory=lambda: float(_env("STOP_POLL", "1")))
    # Worker identity and pools (used by `python -m app.worker` and by the embedded worker).
    # See default_worker_name(): AHQ_WORKER_NAME, or e.g. worker-shockwave-7090 in Docker.
    worker_name: str = field(default_factory=default_worker_name)
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
