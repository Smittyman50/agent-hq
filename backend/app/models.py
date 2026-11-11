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
    # Slots: at most this many model requests to this provider at once, across every
    # worker (0 = no limit). Extra requests wait their turn. Use it to keep a GPU box from
    # being swamped when several agents and sub-agents run together.
    max_concurrent: int = 0
    enabled: bool = True
    created_at: datetime = Field(default_factory=_now)


class SubagentMode(str, Enum):
    off = "off"          # can't create sub-agents
    ask = "ask"          # may ask you; spawns only within limits you approve
    allowed = "allowed"  # may spawn within its own limits (set in the agent's settings)


def default_subagent_limits() -> dict[str, Any]:
    return {
        "concurrent": 1,      # sub-agents of this agent alive at once
        "per_task": 2,        # sub-agents per job (one turn or one delegated task)
        "per_day": 5,         # sub-agents per day (UTC)
        "max_minutes": 10,    # a sub-agent's run is stopped after this long
        "max_requests": 25,   # model requests a sub-agent may make
        # [{"provider_id", "model"}] sub-agents may run on. Empty = the agent's own model.
        "models": [],
        # Tool servers sub-agents may use: a subset of the agent's own. None = all of them.
        "tool_server_ids": None,
    }


class LearningMode(str, Enum):
    off = "off"          # no skill proposals from this agent
    propose = "propose"  # proposals wait for your approval (the default)
    auto = "auto"        # proposals are applied at once; you're told


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
    # Image attachments go straight to this agent's own model (it must be multimodal).
    # Off: the agent asks its vision model about them with look_at_attachment instead.
    sees_images: bool = False
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
    use_profile: bool = True  # put your profile (Settings → Profile) in its instructions
    # Context window in tokens (None = ask the provider; see app/context.py).
    context_window: int | None = None
    # Skills: whether this agent may propose new skills or improvements (see app/skills.py).
    learning_mode: LearningMode = LearningMode.propose
    # Sub-agents: off (default), ask (needs your approval), allowed (within limits).
    subagent_mode: SubagentMode = SubagentMode.off
    subagent_limits: dict[str, Any] = Field(default_factory=default_subagent_limits,
                                            sa_column=Column(JSON))
    # Set on sub-agents themselves: temporary agents created by another agent for one task.
    # They are hidden from agent lists, can't delegate or spawn, and retire when done.
    is_subagent: bool = Field(default=False, index=True)
    parent_agent_id: str | None = Field(default=None, index=True)
    spawned_by_job_id: str | None = None
    expires_at: datetime | None = None
    retired: bool = False
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
    # Approvals, per tool: {"run_shell": "ask", "*": "allow"} with modes allow | ask | block.
    # A tool with no rule (and no "*") uses what the server suggests in the tool's MCP
    # metadata ("agent_hq/approval"), else allow.
    approval_rules: dict[str, str] = Field(default_factory=dict, sa_column=Column(JSON))
    # Evals: tools that may run for real in a test when a case has no recording for the call
    # ({"list_vms": true}); unset tools follow the server's read-only hint. Only tools set to
    # Allow ever run in a test.
    eval_live: dict[str, bool] = Field(default_factory=dict, sa_column=Column(JSON))
    enabled: bool = True
    created_at: datetime = Field(default_factory=_now)


class Skill(SQLModel, table=True):
    """Written-down know-how: when to use it, the steps, the pitfalls. Versioned."""

    id: str = Field(default_factory=_id, primary_key=True)
    name: str = Field(index=True, sa_column_kwargs={"unique": True})  # slug, e.g. check_pve_backups
    title: str = ""
    when_to_use: str = Field(default="", sa_column=Column(Text, default=""))
    body: str = Field(default="", sa_column=Column(Text, default=""))  # the procedure (markdown)
    tools: list[str] = Field(default_factory=list, sa_column=Column(JSON))  # tools it relies on
    script: str | None = None  # a saved workbench script it uses, if any
    # team = every agent; agent = only agent_id
    scope: str = Field(default="team", index=True)
    agent_id: str | None = Field(default=None, index=True)
    status: str = Field(default="active", index=True)  # active | retired
    version: int = 1
    created_by: str = ""
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class SkillVersion(SQLModel, table=True):
    """Every version of every skill, for history and rollback."""

    id: str = Field(default_factory=_id, primary_key=True)
    skill_id: str = Field(index=True)
    version: int
    title: str = ""
    when_to_use: str = Field(default="", sa_column=Column(Text, default=""))
    body: str = Field(default="", sa_column=Column(Text, default=""))
    tools: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    script: str | None = None
    created_by: str = ""
    note: str = Field(default="", sa_column=Column(Text, default=""))  # why it changed
    created_at: datetime = Field(default_factory=_now)


class SkillProposal(SQLModel, table=True):
    """An agent proposing a new skill or a change to one."""

    id: str = Field(default_factory=_id, primary_key=True)
    skill_id: str | None = Field(default=None, index=True)  # set = a revision of that skill
    name: str = ""
    title: str = ""
    when_to_use: str = Field(default="", sa_column=Column(Text, default=""))
    body: str = Field(default="", sa_column=Column(Text, default=""))
    tools: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    script: str | None = None
    scope: str = "team"
    reason: str = Field(default="", sa_column=Column(Text, default=""))
    source: str = "agent"  # agent (propose_skill) | reflection | document
    # Evals: the run that tested this change, and its numbers.
    eval_run_id: str | None = None
    eval_summary: dict = Field(default_factory=dict, sa_column=Column(JSON))
    agent_id: str = Field(index=True)
    agent_name: str = ""
    thread_id: str | None = None
    job_id: str | None = None
    base_version: int | None = None  # the version a revision was written against
    # pending | approved | rejected | applied (auto mode)
    status: str = Field(default="pending", index=True)
    note: str = Field(default="", sa_column=Column(Text, default=""))
    message_id: str | None = None
    created_at: datetime = Field(default_factory=_now)
    decided_at: datetime | None = None


class SkillUse(SQLModel, table=True):
    """An agent loading a skill during a job, and how that job turned out."""

    id: str = Field(default_factory=_id, primary_key=True)
    skill_id: str = Field(index=True)
    version: int = 1
    agent_id: str = ""
    job_id: str | None = Field(default=None, index=True)
    thread_id: str | None = None
    outcome: str = Field(default="pending", index=True)  # pending | done | partial | failed
    rating: str | None = None  # your rating of the reply that came out of the job: up | down
    at: datetime = Field(default_factory=_now)


class KnowledgeCollection(SQLModel, table=True):
    """A shelf in the knowledge library: documents from one or more sources, readable by
    the agents you choose (none chosen = every agent)."""

    id: str = Field(default_factory=_id, primary_key=True)
    name: str = Field(index=True)
    description: str = Field(default="", sa_column=Column(Text, default=""))
    agent_ids: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    auto_recall: bool = True   # matching excerpts come up by themselves, like memories
    enabled: bool = True
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class KnowledgeSource(SQLModel, table=True):
    """Where a collection's documents come from: uploaded files, a web page, or a Git repo
    (an Obsidian vault pushed with the Obsidian Git plugin, runbooks, docs)."""

    id: str = Field(default_factory=_id, primary_key=True)
    collection_id: str = Field(index=True)
    kind: str = "upload"  # upload | url | git
    name: str = ""
    url: str | None = None          # web page, or the repo's clone URL
    branch: str = ""                # git: blank = the repo's default branch
    username: str = ""              # git: the account the token belongs to
    token: str | None = None        # git: access token (never sent back to the browser)
    include: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    exclude: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    obsidian: bool = False          # understand frontmatter, #tags, [[links]]; obsidian:// links
    vault_name: str = ""            # for obsidian:// links (blank = the repo's name)
    refresh_minutes: int = 0        # check for changes this often (0 = only when you ask)
    status: str = Field(default="queued", index=True)  # queued | indexing | ready | error
    error: str | None = Field(default=None, sa_column=Column(Text))
    last_indexed_at: datetime | None = None
    last_commit: str = ""
    docs: int = 0
    chunks: int = 0
    created_at: datetime = Field(default_factory=_now)


class KnowledgeDoc(SQLModel, table=True):
    """One document (a note, a file, a page) as text."""

    id: str = Field(default_factory=_id, primary_key=True)
    source_id: str = Field(index=True)
    collection_id: str = Field(index=True)
    path: str = Field(index=True)   # path in the repo, file name, or URL
    title: str = ""
    link: str = ""                  # where to open it: obsidian://, the web page, the repo
    tags: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    links: list[str] = Field(default_factory=list, sa_column=Column(JSON))  # [[wikilinks]]
    text: str = Field(default="", sa_column=Column(Text, default=""))
    hash: str = ""
    indexed_hash: str = ""          # the text that its chunks and embeddings come from
    size: int = 0
    chunks: int = 0
    stored_file: str | None = None  # uploads: the original, under the data folder
    updated_at: datetime = Field(default_factory=_now)


class KnowledgeChunk(SQLModel, table=True):
    """A section of a document: what search finds and agents get as excerpts."""

    id: str = Field(default_factory=_id, primary_key=True)
    doc_id: str = Field(index=True)
    source_id: str = Field(index=True)
    collection_id: str = Field(index=True)
    ord: int = 0
    heading: str = ""               # "Backups › Proxmox › Verify jobs"
    text: str = Field(sa_column=Column(Text))
    embedding: list[float] | None = Field(default=None, sa_column=Column(JSON))  # without pgvector
    embedding_model: str | None = None


class JobTrace(SQLModel, table=True):
    """A job's tool calls with their full arguments and results (kept for a couple of weeks)
    so a reply can become a test case that replays them."""

    job_id: str = Field(primary_key=True)
    steps: list[dict] = Field(default_factory=list, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_now, index=True)


class EvalCase(SQLModel, table=True):
    """A test: a request for an agent, checks its answer must pass, and the tool results
    recorded from the original job (replayed, so a test never touches your lab)."""

    id: str = Field(default_factory=_id, primary_key=True)
    name: str = ""
    agent_id: str = Field(index=True)
    kind: str = "turn"   # turn (a chat message) | task (delegated work)
    prompt: str = Field(sa_column=Column(Text))
    context: str = Field(default="", sa_column=Column(Text, default=""))  # earlier conversation
    # [{"type": "rubric" | "contains" | "not_contains" | "tool_called" | "tool_not_called"
    #   | "skill_loaded", "value": "..."}]
    checks: list[dict] = Field(default_factory=list, sa_column=Column(JSON))
    recording: list[dict] = Field(default_factory=list, sa_column=Column(JSON))
    recording_partial: bool = False  # only abbreviated results were left to record
    skill_names: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    status: str = Field(default="draft", index=True)  # draft | active | archived
    source: str = "manual"  # manual | reply | feedback | correction
    source_job_id: str | None = Field(default=None, index=True)
    source_message_id: str | None = None
    thread_id: str | None = None
    last_passed: bool | None = None
    last_run_at: datetime | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class EvalRun(SQLModel, table=True):
    """A batch of test cases, run as they are, or with and without a skill change."""

    id: str = Field(default_factory=_id, primary_key=True)
    trigger: str = "manual"  # manual | proposal
    label: str = ""
    case_ids: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    variants: list[str] = Field(default_factory=lambda: ["current"], sa_column=Column(JSON))
    proposal_id: str | None = Field(default=None, index=True)
    status: str = Field(default="queued", index=True)  # queued | running | done | failed
    # {"current": {"passed": 5, "total": 8}} or {"without": {...}, "with": {...}}
    summary: dict = Field(default_factory=dict, sa_column=Column(JSON))
    judge: str = ""
    error: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=_now, index=True)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class EvalResult(SQLModel, table=True):
    id: str = Field(default_factory=_id, primary_key=True)
    run_id: str = Field(index=True)
    case_id: str = Field(index=True)
    variant: str = "current"
    passed: bool = False
    reply: str = Field(default="", sa_column=Column(Text, default=""))
    # [{"check": {...}, "passed": bool, "note": "..."}]
    checks: list[dict] = Field(default_factory=list, sa_column=Column(JSON))
    calls: list[dict] = Field(default_factory=list, sa_column=Column(JSON))  # what it did, and how answered
    model: str = ""
    seconds: float = 0
    error: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=_now)


class ProfileProposal(SQLModel, table=True):
    """An agent suggesting an addition to your profile (Settings → Profile), from a reflection.
    Your profile goes into every agent's instructions, so changes always wait for you."""

    id: str = Field(default_factory=_id, primary_key=True)
    text: str = Field(sa_column=Column(Text))
    reason: str = Field(default="", sa_column=Column(Text, default=""))
    source: str = "reflection"
    agent_id: str = Field(index=True)
    agent_name: str = ""
    thread_id: str | None = None
    job_id: str | None = None
    reflection_id: str | None = None
    message_id: str | None = None
    status: str = Field(default="pending", index=True)  # pending | approved | rejected
    note: str = ""
    created_at: datetime = Field(default_factory=_now)
    decided_at: datetime | None = None


class Feedback(SQLModel, table=True):
    """Your 👍 / 👎 on an agent's reply, with an optional note (what it should have done)."""

    id: str = Field(default_factory=_id, primary_key=True)
    message_id: str = Field(index=True)
    thread_id: str = Field(index=True)
    job_id: str | None = Field(default=None, index=True)
    agent_id: str | None = Field(default=None, index=True)
    rating: str = "up"  # up | down
    comment: str = Field(default="", sa_column=Column(Text, default=""))
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class Reflection(SQLModel, table=True):
    """An agent looking back at a finished job to learn from it: new or better skills
    (as proposals, under its learning mode) and lessons for its memory."""

    id: str = Field(default_factory=_id, primary_key=True)
    job_id: str = Field(index=True)
    thread_id: str = Field(index=True)
    agent_id: str = Field(index=True)
    agent_name: str = ""
    # Why it's worth a look: ["failed", "6 tool calls", "you rated it down", ...]
    reasons: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    # waiting | running | done | skipped | failed
    status: str = Field(default="waiting", index=True)
    due_at: datetime = Field(default_factory=_now, index=True)
    summary: str = Field(default="", sa_column=Column(Text, default=""))
    proposal_ids: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    memory_ids: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    profile_proposal_ids: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    model: str = ""
    error: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ToolApproval(SQLModel, table=True):
    """A tool call waiting for (or decided by) you, under an "ask" approval rule."""

    id: str = Field(default_factory=_id, primary_key=True)
    agent_id: str = Field(index=True)
    agent_name: str = ""
    thread_id: str = Field(index=True)
    job_id: str | None = None
    server_id: str = Field(index=True)
    server_name: str = ""
    tool: str = Field(index=True)
    args: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # pending | approved | denied | expired
    status: str = Field(default="pending", index=True)
    note: str = Field(default="", sa_column=Column(Text, default=""))
    # On approval: don't ask again for this tool, agent and conversation until then.
    remember_until: datetime | None = None
    message_id: str | None = None
    created_at: datetime = Field(default_factory=_now)
    decided_at: datetime | None = None


class Thread(SQLModel, table=True):
    """A conversation between you and one agent (the thread's owner)."""

    id: str = Field(default_factory=_id, primary_key=True)
    agent_id: str = Field(foreign_key="agentdef.id", index=True)
    title: str = "New conversation"
    created_at: datetime = Field(default_factory=_now)


class Schedule(SQLModel, table=True):
    """A task an agent runs on a schedule. Each run is a turn in the schedule's own
    conversation, so results show up in the sidebar like any chat."""

    id: str = Field(default_factory=_id, primary_key=True)
    name: str
    agent_id: str = Field(foreign_key="agentdef.id", index=True)
    task: str = Field(default="", sa_column=Column(Text, default=""))
    # hourly | daily | weekly | monthly | cron. The friendly fields below are kept so the
    # editor can show them again; `cron` is what actually drives the schedule.
    frequency: str = "daily"
    interval_hours: int = 1          # hourly: every N hours
    minute: int = 0
    hour: int = 9
    weekdays: list[int] = Field(default_factory=lambda: [1], sa_column=Column(JSON))  # 0=Sun … 6=Sat
    day_of_month: int = 1
    cron: str = "0 9 * * *"
    timezone: str = "UTC"            # IANA name, e.g. America/New_York
    enabled: bool = True
    skip_if_running: bool = True     # don't start a run while the previous one is still going
    thread_id: str | None = None     # the conversation runs are posted to (created on first run)
    next_run_at: datetime | None = Field(default=None, index=True)  # UTC
    last_run_at: datetime | None = None
    # Recent runs, newest first: [{at, job_id, trigger, skipped?}] (status comes from the job).
    runs: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
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


class Attachment(SQLModel, table=True):
    """A file in a conversation: one you attached, or one an agent made or sent back.

    The bytes live in the database (like avatars), so workers on any machine can read
    them. Text is pulled out of documents once, when the file arrives.
    """

    id: str = Field(default_factory=_id, primary_key=True)
    thread_id: str = Field(foreign_key="thread.id", index=True)
    # The message it belongs to (None = uploaded, not sent yet; or made during a run
    # whose message isn't written yet).
    message_id: str | None = Field(default=None, index=True)
    source: str = "user"           # user | agent | workbench
    agent_id: str | None = None    # who made it, for agent files
    job_id: str | None = Field(default=None, index=True)  # the run that made it
    name: str
    mime: str = "application/octet-stream"
    kind: str = "file"             # image | document | text | file
    size: int = 0
    sha256: str = ""
    width: int = 0                 # images
    height: int = 0
    data: bytes = Field(sa_column=Column(LargeBinary, nullable=False))
    text: str | None = Field(default=None, sa_column=Column(Text))  # extracted text, if any
    text_error: str = ""           # why there is no text, when extraction failed
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


class User(SQLModel, table=True):
    """A person who can sign in to the web UI. (Agents and workers never sign in.)"""

    id: str = Field(default_factory=_id, primary_key=True)
    username: str = Field(index=True, sa_column_kwargs={"unique": True})
    password_hash: str
    # TOTP two-factor: base32 secret once confirmed; pending_totp holds one being set up.
    totp_secret: str | None = None
    pending_totp: str | None = None
    is_admin: bool = True
    created_at: datetime = Field(default_factory=_now)
    password_changed_at: datetime = Field(default_factory=_now)
    last_login_at: datetime | None = None


class AuthSession(SQLModel, table=True):
    """A signed-in browser. The cookie holds a random token; only its hash is stored."""

    id: str = Field(default_factory=_id, primary_key=True)
    token_hash: str = Field(index=True, sa_column_kwargs={"unique": True})
    user_id: str = Field(foreign_key="user.id", index=True)
    created_at: datetime = Field(default_factory=_now)
    last_seen_at: datetime = Field(default_factory=_now)
    expires_at: datetime
    ip: str = ""
    user_agent: str = ""


class ApiToken(SQLModel, table=True):
    """A personal API token for scripts (Authorization: Bearer ahq_...). Hash only."""

    id: str = Field(default_factory=_id, primary_key=True)
    user_id: str = Field(foreign_key="user.id", index=True)
    name: str
    prefix: str = Field(index=True)  # the visible part, e.g. ahq_3f9a1c2b, to tell tokens apart
    token_hash: str = Field(sa_column_kwargs={"unique": True})
    created_at: datetime = Field(default_factory=_now)
    last_used_at: datetime | None = None
    expires_at: datetime | None = None


class AuthEvent(SQLModel, table=True):
    """Sign-in audit log: logins, failures, lockouts, password/2FA/token changes."""

    id: str = Field(default_factory=_id, primary_key=True)
    at: datetime = Field(default_factory=_now, index=True)
    kind: str = Field(index=True)
    username: str = ""
    ip: str = ""
    user_agent: str = ""
    detail: str = ""


class SystemSetting(SQLModel, table=True):
    """Small key/value store for settings edited in the UI (e.g. memory settings)."""

    key: str = Field(primary_key=True)
    value: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


class SubagentRequest(SQLModel, table=True):
    """An agent asking you for sub-agents (like an FTE request): what, why, how much."""

    id: str = Field(default_factory=_id, primary_key=True)
    agent_id: str = Field(foreign_key="agentdef.id", index=True)
    thread_id: str = Field(foreign_key="thread.id", index=True)
    job_id: str | None = None
    why: str = Field(default="", sa_column=Column(Text, default=""))
    why_not_existing_agents: str = Field(default="", sa_column=Column(Text, default=""))
    # [{role, purpose, model, provider_id, tools, est_minutes}]
    subagents: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    hours: float = 4  # how long the agent wants the grant for
    # The system's own estimate of the cost: models, sizes, where they run, ledger averages.
    estimate: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # pending | approved | denied | expired
    status: str = Field(default="pending", index=True)
    # What you approved: {count, concurrent, max_minutes, max_requests, models, tool_server_ids}
    grant: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    used: int = 0
    note: str = Field(default="", sa_column=Column(Text, default=""))  # your note to the agent
    message_id: str | None = None
    expires_at: datetime | None = None  # grant end (approved) or when an unanswered request lapses
    decided_at: datetime | None = None
    created_at: datetime = Field(default_factory=_now)


class UsageRecord(SQLModel, table=True):
    """The ledger: one row per agent run (a turn, a delegated task or a sub-agent task)."""

    id: str = Field(default_factory=_id, primary_key=True)
    at: datetime = Field(default_factory=_now, index=True)
    agent_id: str = Field(index=True)
    agent_name: str = ""
    is_subagent: bool = False
    parent_agent_id: str | None = None   # for sub-agents: the agent that created them
    job_id: str | None = None
    thread_id: str | None = None
    provider_id: str | None = None
    provider_name: str = ""
    model: str = ""
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0
    ok: bool = True


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
