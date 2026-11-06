"""Database tables.

Everything that defines an agent lives here as data. No agent name, role or
capability appears anywhere in the code: the web UI creates and edits these
rows, and the runtime builds agents from them on every run.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


def _id() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AgentDef(SQLModel, table=True):
    id: str = Field(default_factory=_id, primary_key=True)
    name: str = Field(index=True, unique=True)
    # Shown to other agents when they decide whom to delegate to.
    description: str = ""
    # The system instructions for this agent.
    instructions: str = ""
    # Model spec, e.g. "openai:gpt-4.1", "ollama:gpt-oss:20b", "anthropic:claude-sonnet-4-5".
    model: str = "ollama:gpt-oss:20b"
    # IDs of ToolServer rows this agent may use.
    tool_server_ids: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    # IDs of agents this agent may delegate to. ["*"] means every enabled agent.
    delegates: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    # Optional URL of this agent's desktop viewer (KasmVNC/noVNC), shown in the UI.
    desktop_url: str | None = None
    # The agent new chats open with, if any. Purely a UI default.
    is_default: bool = False
    enabled: bool = True
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class Transport(str, Enum):
    http = "http"    # streamable HTTP / SSE URL
    stdio = "stdio"  # local command launched by the backend


class ToolServer(SQLModel, table=True):
    """An MCP server. Adding a capability = registering one of these."""

    id: str = Field(default_factory=_id, primary_key=True)
    name: str = Field(index=True, unique=True)
    description: str = ""
    transport: Transport = Transport.http
    url: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    env: dict[str, str] = Field(default_factory=dict, sa_column=Column(JSON))
    # Prefix tool names with the server name to avoid collisions between servers.
    prefix_tools: bool = True
    enabled: bool = True
    created_at: datetime = Field(default_factory=_now)


class Thread(SQLModel, table=True):
    """A conversation between you and one agent (the thread's owner)."""

    id: str = Field(default_factory=_id, primary_key=True)
    agent_id: str = Field(foreign_key="agentdef.id", index=True)
    title: str = "New conversation"
    created_at: datetime = Field(default_factory=_now)


class Role(str, Enum):
    user = "user"
    agent = "agent"
    event = "event"  # delegation started/finished, job reports, errors


class Message(SQLModel, table=True):
    """Human-readable transcript shown in the UI.

    The model-level history (tool calls etc.) lives in the LangGraph checkpoint.
    """

    id: str = Field(default_factory=_id, primary_key=True)
    thread_id: str = Field(foreign_key="thread.id", index=True)
    role: Role
    agent_id: str | None = None
    content: str
    meta: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_now)


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"


class Job(SQLModel, table=True):
    """A delegated task. Always remembers the conversation it came from."""

    id: str = Field(default_factory=_id, primary_key=True)
    origin_thread_id: str = Field(foreign_key="thread.id", index=True)
    requester_agent_id: str
    assignee_agent_id: str
    parent_job_id: str | None = None
    task: str
    background: bool = False
    status: JobStatus = JobStatus.queued
    result: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    error: str | None = None
    created_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None
