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

from sqlalchemy import JSON, Column, LargeBinary, Text
from sqlmodel import Field, SQLModel


def _id() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ProviderKind(str, Enum):
    ollama = "ollama"                        # Ollama server (models listed from /api/tags)
    openai = "openai"                        # OpenAI
    openai_compatible = "openai_compatible"  # vLLM, LM Studio, LiteLLM, llama.cpp server, ...
    anthropic = "anthropic"                  # Anthropic
    chatgpt = "chatgpt"                      # your ChatGPT Plus/Pro plan, via Sign in with ChatGPT


class ModelProvider(SQLModel, table=True):
    """Somewhere models come from. Agents pick a provider, then one of its models."""

    id: str = Field(default_factory=_id, primary_key=True)
    name: str = Field(index=True, unique=True)
    kind: ProviderKind = ProviderKind.ollama
    # Ollama: http://host:11434   OpenAI-compatible: http://host:8000/v1   Others: optional override
    base_url: str | None = None
    # Optional. Empty = workers use the provider's usual environment variable
    # (OPENAI_API_KEY, ANTHROPIC_API_KEY). Never returned by the API.
    api_key: str | None = None
    # ChatGPT plan providers: the Sign in with ChatGPT registration and tokens
    # (client_id, access/refresh/id tokens, granted scopes). Never returned by the API.
    oauth: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    enabled: bool = True
    created_at: datetime = Field(default_factory=_now)


class AgentDef(SQLModel, table=True):
    id: str = Field(default_factory=_id, primary_key=True)
    name: str = Field(index=True, unique=True)
    # Shown to other agents when they decide whom to delegate to.
    description: str = Field(default="", sa_column=Column(Text, default=""))
    # How teammates refer to this agent, e.g. "he/him", "she/her", "they/them".
    pronouns: str = ""
    # The system instructions for this agent.
    instructions: str = Field(default="", sa_column=Column(Text, default=""))
    # The model provider (a ModelProvider row) and a model name it offers, e.g. "qwen3:14b".
    # With no provider, `model` is a raw PydanticAI spec such as "openai:gpt-4.1" (advanced).
    provider_id: str | None = Field(default=None, foreign_key="modelprovider.id")
    model: str = ""
    # Optional vision model. When set, the agent never sees screenshots itself: it gets
    # look / locate / click_on tools, and this model looks at the screen for it.
    vision_provider_id: str | None = Field(default=None, foreign_key="modelprovider.id")
    vision_model: str = ""
    # How the vision model reports positions: "relative_1000" (0-1000 scale, Qwen-VL
    # family, Holo) or "pixels" (absolute pixels of the image it was given, UI-TARS).
    vision_coords: str = "relative_1000"
    # Re-check each located point on a zoomed-in crop (slower, more accurate).
    vision_zoom: bool = True
    # Optional fallback model, used when the main model fails or the agent escalates.
    fallback_provider_id: str | None = Field(default=None, foreign_key="modelprovider.id")
    fallback_model: str = ""
    # Which worker pool runs this agent. Workers subscribe to one or more pools, so
    # this is how you pin an agent to a machine (e.g. the host next to its desktop).
    pool: str = "default"
    # IDs of ToolServer rows this agent may use.
    tool_server_ids: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    # IDs of agents this agent may delegate to. ["*"] means every enabled agent.
    delegates: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    # Optional URL of this agent's desktop viewer (KasmVNC/noVNC), shown in the UI.
    desktop_url: str | None = None
    # The agent new chats open with, if any. Purely a UI default.
    is_default: bool = False
    # Use long-term memory: recall before each job, memory tools, automatic capture.
    memory_enabled: bool = True
    # Bumped on every avatar upload; 0 = no avatar. The UI uses it to bust image caches.
    avatar_version: int = 0
    avatar_animated: bool = False
    enabled: bool = True
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class AgentAvatar(SQLModel, table=True):
    """An agent's avatar image, stored in the database so every machine sees it."""

    agent_id: str = Field(foreign_key="agentdef.id", primary_key=True)
    content_type: str
    data: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    # First frame as a small PNG, for when animation is turned off.
    still: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    width: int = 0
    height: int = 0
    frames: int = 1
    updated_at: datetime = Field(default_factory=_now)


class Transport(str, Enum):
    http = "http"    # streamable HTTP / SSE URL
    stdio = "stdio"  # local command launched by the worker running the agent


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
    # This server drives an agent desktop. Its tools pause while you have control of the
    # desktop, and agents using it get the ask_user_to_take_over tool.
    desktop: bool = False
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
    event = "event"  # delegation results, MQTT events, job reports, errors


class Message(SQLModel, table=True):
    """Human-readable transcript shown in the UI.

    The model-level history (tool calls etc.) lives in the LangGraph checkpoint.
    """

    id: str = Field(default_factory=_id, primary_key=True)
    thread_id: str = Field(foreign_key="thread.id", index=True)
    role: Role
    agent_id: str | None = None
    content: str = Field(sa_column=Column(Text, nullable=False))
    meta: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_now)


class JobKind(str, Enum):
    turn = "turn"  # one turn of a conversation, run by the thread's owner agent
    task = "task"  # a delegated task, run by the assignee, returns a WorkResult


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"


class Job(SQLModel, table=True):
    """A unit of work delivered to workers through a Valkey stream.

    Always remembers the conversation it came from, so results find their way back.
    """

    id: str = Field(default_factory=_id, primary_key=True)
    kind: JobKind = JobKind.task
    origin_thread_id: str = Field(foreign_key="thread.id", index=True)
    requester_agent_id: str | None = None  # None for turns started by you or an event
    assignee_agent_id: str
    parent_job_id: str | None = None
    task: str = Field(sa_column=Column(Text, nullable=False))
    # True: report the result into the origin thread when done (background delegation).
    background: bool = False
    # Delegation-chain context, so a job can run on any machine.
    depth: int = 0
    chain: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    pool: str = "default"
    # W3C traceparent of the span that queued this job, so tracing continues on the worker.
    trace_parent: str | None = None
    # Where a turn came from, e.g. {"kind": "mqtt", "topic": ..., "payload": ...}.
    meta: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    status: JobStatus = JobStatus.queued
    attempts: int = 0
    worker: str | None = None
    result: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    error: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class SystemSetting(SQLModel, table=True):
    """Small key/value store for settings edited in the UI (e.g. memory settings)."""

    key: str = Field(primary_key=True)
    value: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class MemoryKind(str, Enum):
    fact = "fact"              # something true about the environment
    incident = "incident"      # something that happened and how it was handled
    preference = "preference"  # how the user wants things done
    procedure = "procedure"    # how to do something
    note = "note"


class MemoryScope(str, Enum):
    team = "team"      # visible to every agent
    agent = "agent"    # visible only to one agent
    thread = "thread"  # visible only within one conversation


class Memory(SQLModel, table=True):
    """Long-term memory. Never silently overwritten: outdated memories are retired
    (invalidated_at) and optionally point at the memory that replaced them."""

    id: str = Field(default_factory=_id, primary_key=True)
    text: str = Field(sa_column=Column(Text, nullable=False))
    kind: MemoryKind = MemoryKind.fact
    scope: MemoryScope = MemoryScope.team
    agent_id: str | None = Field(default=None, index=True)
    thread_id: str | None = Field(default=None, index=True)
    # auto:delegation, auto:event, agent, user, tidy
    source: str = "user"
    source_job_id: str | None = None
    created_by: str | None = None  # agent ID, or None for you
    valid_from: datetime = Field(default_factory=_now)
    invalidated_at: datetime | None = Field(default=None, index=True)
    invalidated_reason: str | None = None
    superseded_by: str | None = None
    last_confirmed: datetime = Field(default_factory=_now)
    times_seen: int = 1
    # Embedding (portable copy; on Postgres with pgvector the vector column is used instead).
    embedding: list[float] | None = Field(default=None, sa_column=Column(JSON))
    embedding_model: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class Subscription(SQLModel, table=True):
    """Wakes an agent when a matching MQTT message arrives."""

    id: str = Field(default_factory=_id, primary_key=True)
    name: str = Field(index=True, unique=True)
    # MQTT topic filter; + and # wildcards allowed, e.g. homelab/alerts/#
    topic_filter: str
    agent_id: str = Field(foreign_key="agentdef.id")
    # Conversation the events go to. Empty = a dedicated thread is created on first event.
    thread_id: str | None = None
    # {topic} and {payload} are filled in. Empty = the default prompt.
    prompt_template: str = Field(default="", sa_column=Column(Text, default=""))
    # Ignore further matches for this long after one fires (alert-storm protection).
    min_interval_seconds: int = 60
    enabled: bool = True
    created_at: datetime = Field(default_factory=_now)
