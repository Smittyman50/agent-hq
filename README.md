# Agent HQ

A self-hosted multi-agent system. You create agents in the web UI and give each one instructions, a model, tools and delegation rights. One agent can supervise others, and delegated work always reports back to the conversation it came from.

- **Backend:** FastAPI, LangGraph (conversation graph + checkpointing), PydanticAI (agents, typed results, MCP tools)
- **Frontend:** Angular 22 (signals, standalone components)
- **Tools:** any MCP server, registered in the UI
- **Desktops:** one `linuxserver/webtop` container per agent, driven by a small MCP server, viewable in the UI

Nothing about specific agents is in the code. Names, roles, instructions, models, tools and who-can-delegate-to-whom are all database rows. Renaming or re-tasking an agent takes effect on its next message.

## How delegation works

Agents that are allowed to delegate get two tools: `delegate(agent_name, task, background=False)` and `job_status(job_id)`. Their instructions get the current team roster (names and descriptions) appended automatically, so a new agent shows up for its supervisor right away.

- **Wait mode (default).** The delegate runs right away and returns a structured `WorkResult` (`status`, `summary`, `details`) as the tool's return value. The supervisor always has the result in hand when it answers you.
- **Background mode.** The tool returns a job ID at once. Every job stores its `origin_thread_id`. When the job finishes, the result is posted into that conversation and the conversation's owner agent is woken to report it to you.
- **Failures are results too.** Model errors, exceptions and server restarts all produce a `failed` WorkResult that gets reported. Background jobs killed by a restart are reported when the server starts again.
- **Guards.** You can only delegate to agents on your list, a chain can't loop back to an agent already in it, and nesting depth is capped (`AHQ_MAX_DELEGATION_DEPTH`).
- **Visibility.** Every delegation and tool call streams to the UI's live activity panel and is recorded in the job table. With OpenTelemetry on, it also shows up as spans in Phoenix.

## Quick start (dev)

```bash
# backend
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp ../.env.example ../.env   # edit, then export or use direnv
uvicorn app.main:app --reload --port 8000

# frontend (another shell)
cd frontend
npm install
npx ng serve                 # http://localhost:4200, proxies /api and /ws to :8000
```

To try the example crew (the agents are data, in `examples/agents.example.json`), go to **Agents → Import** and pick that file. Edit the models and URLs to match your setup.

Run the tests with `cd backend && pytest`. They use scripted fake models, so no LLM is needed. They cover wait and background delegation, failure reporting, renaming, a real stdio MCP server, and import/export.

## Deploy (Docker)

```bash
cp .env.example .env         # fill in Ollama URL, API keys, OTLP endpoint
docker compose up -d --build
```

- UI + API: `http://<host>:8000`
- Example desktop: `http://<host>:3000`. In the UI, register the tool server `http://desktop-1:8765/mcp` (HTTP), tick it on the agent that owns the desktop, and set that agent's *Desktop viewer URL*.

To add another desktop, copy the `desktop-1` block in `docker-compose.yml` (`desktop-2`, host port `3002:3000`, its own `/config` volume) and register `http://desktop-2:8765/mcp`.

## Adding tools

Write or reuse an MCP server and register it in **Tools**:

- **HTTP:** a URL like `http://perceptor:8811/mcp`
- **stdio:** a command plus arguments, launched by the backend, e.g. `uvx` with `mcp-server-fetch`

Click **Test connection** to list its tools, then tick the server on any agent. Tool names are prefixed with the server name by default (`proxmox_list_vms`) so they never collide.

## Model specs

| Spec | Goes to |
|---|---|
| `ollama:gpt-oss:20b` | `AHQ_OLLAMA_BASE_URL` |
| `openai:gpt-4.1` | OpenAI (`OPENAI_API_KEY`) |
| `anthropic:<model>` | Anthropic (`pip install ".[anthropic]"`, `ANTHROPIC_API_KEY`) |
| `test` | PydanticAI's built-in test model (no LLM, for UI checks) |

Agents that use the desktop tools need a vision-capable model.

## Layout

```
backend/app/
  models.py          AgentDef, ToolServer, Thread, Message, Job tables
  runtime.py         builds agents from rows; delegation; background jobs; LangGraph turn graph
  llm.py             model spec -> PydanticAI model
  api/               REST + WebSocket (/ws/threads/{id})
  telemetry.py       OTLP -> your Collector -> Phoenix
frontend/src/app/    chat, agents and tools pages
desktop/             webtop image + desktop MCP server (screenshot/click/type/key/scroll/open_url)
examples/            example agent crew as importable JSON
```

## Next steps worth building

- **Approvals:** mark tools as needing approval (PydanticAI `requires_approval` / LangGraph interrupts) and show approve/deny cards in chat before anything changes a system.
- **Schedules:** a `Schedule` table (cron + agent + prompt + origin thread). A scheduled run is just a background job, so it reports back the same way.
- **Postgres:** set `AHQ_DB_URL` and swap in `langgraph-checkpoint-postgres` when you outgrow SQLite.
- **Auth:** put it behind your reverse proxy or step-ca mTLS before exposing it beyond the LAN.
