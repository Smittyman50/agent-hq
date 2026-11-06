"""Runtime settings, read from environment variables (prefix AHQ_)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(f"AHQ_{name}", default)


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "./data")))
    # Base URL of your Ollama server (OpenAI-compatible /v1 endpoint).
    ollama_base_url: str = field(default_factory=lambda: _env("OLLAMA_BASE_URL", "http://localhost:11434/v1"))
    # How deep a delegation chain may go (A -> B -> C ...) before it is refused.
    max_delegation_depth: int = field(default_factory=lambda: int(_env("MAX_DELEGATION_DEPTH", "4")))
    # Allowed browser origins for the Angular dev server.
    cors_origins: list[str] = field(
        default_factory=lambda: _env("CORS_ORIGINS", "http://localhost:4200").split(",")
    )

    @property
    def db_url(self) -> str:
        return _env("DB_URL", f"sqlite:///{self.data_dir / 'agent_hq.db'}")

    @property
    def checkpoint_path(self) -> str:
        return str(self.data_dir / "checkpoints.db")


settings = Settings()
