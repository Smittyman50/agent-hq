# Development

Running the tests and finding your way around the code. Back to the [overview](../README.md).

## Tests

```bash
cd backend && pytest
cd labtools && pip install -r requirements-dev.txt && pytest   # the lab tool servers
```

The tests use scripted fake models (no LLM needed), a real `valkey-server` or `redis-server` started on a free port, an in-process MQTT broker, and a throwaway Postgres cluster when Postgres is installed. With `AHQ_TEST_DB=postgres`, every test runs on a fresh Postgres database of its own instead of SQLite; with `AHQ_TEST_STRICT=1`, a test that would skip because something it needs isn't installed fails instead. `deploy/test/Dockerfile` has everything installed (Valkey, Postgres 17 + pgvector, Tesseract, Chromium, Node), and deploys run the suite in it on both databases, strict, before anything changes (see [docs/auto-deploy.md](auto-deploy.md)):

```bash
docker build -t agent-hq-test -f deploy/test/Dockerfile .
docker run --rm -e AHQ_TEST_DB=postgres -e AHQ_TEST_STRICT=1 agent-hq-test
``` They cover:
- delegation in wait and background modes
- single-slot workers (no deadlock)
- pinning agents to pools
- **a worker crashing mid-job and another reclaiming it**
- failure reporting
- conversation history persisting across turns
- renaming agents
- a real stdio MCP server
- MQTT matching, cooldowns and a real broker end to end
- the Postgres database and checkpointer with two workers
- long-term memory: keyword and embedding search, scopes, retire/restore/supersede, recall added to instructions, capturing delegations and events without duplicates, agent memory tools, tidy-up with and without a model, re-embedding, and the same search on Postgres + pgvector (via PGlite)
- tracing: one trace across two workers for a turn and its delegation, background reports joining that trace, session IDs and Phoenix span types on every span, and no screenshot data in spans
- avatars: animated GIF served untouched plus a still first frame, validation, keeping them through agent edits and export/import
- local-first models: a real Chromium driven through Playwright MCP over CDP; vision tools on rendered screenshots (with and without zoom, both coordinate formats, and a not-found case); falling back on errors, runaway loops and escalation
- desktop handoff: tools pausing while you have control, an agent asking you to take over and resuming with your note, and the wait timing out
- model providers: listing models for each kind, write-only keys, and two agents on two Ollama models answering through a worker
- thinking levels: the unified setting for OpenAI/Anthropic, `reasoning_effort` for Ollama only when the model can think, the fallback using its own provider, the support hint, an agent run receiving its level, API validation, and export
- MQTT publisher: status and allow-listed publishes reaching a real broker; refusing wildcards, system, reserved and off-list topics, oversized payloads and bursts; and an agent publishing only after you approve
- web reader: feeds (and finding a site's feed), readable pages without clutter, paging of long pages, redirects, robots.txt, SearXNG search, refusing LAN addresses unless allowed, and an agent reading a feed through Agent HQ
- plain-language schedules: about 40 phrases (intervals, windows, daily, days of the week, monthly, nth weekdays, cron) to their fields, cron and description; what can't be scheduled explained; first Mondays, last days and last Fridays landing on the right dates; and the parse API previewing five runs whose fields save as a schedule
- scheduler: every cadence and its description, bad cadences explained, time zones across a DST change, firing into the schedule's own conversation exactly once (two checkers at once), catching up once after downtime, skipping while a run is still going, run now, and disabled agents
- ChatGPT plan providers: the full sign-in by pasted callback (PKCE, state, single use, declined consent), tokens never returned or exported, model listing, an agent turn on the plan through the Responses API (`store: false`, `stream: true`, no unsupported fields), and two workers refreshing at once without ever reusing a refresh token
- Grok plan providers: the device sign-in (pending, slow down, declined, a subscription xAI refuses), tokens never returned or exported, switching kind dropping the sign-in, model listing without image and video models, an agent turn on the plan with its thinking sent as `reasoning_effort`, and two workers refreshing at once without ever reusing a refresh token
- Postgres upgrades: enum values a newer version adds (such as a new provider kind) are added to an older database's enum type on startup
- schema migrations: the revisions building exactly the models' schema (on SQLite and Postgres), adopting a database from before migrations, refusing a database a newer version migrated, waiting instead of migrating, four processes starting at once, ignoring pgvector columns and LangGraph's tables, and a later rename-with-data revision running on startup with a SQLite backup first
- import and export
- deleting agents: only once disabled, never with work in flight, removing what it owns while keeping usage, team memories and skills, and returning its goals to the backlog
- sign-in: setup code, passwords, 2FA, sessions, lockout, API tokens, the audit log, and cross-site refusal
- sub-agents: off/ask/allowed, limits, refusing duplicates of existing agents, requests and grants, provider slots and the usage ledger
- notifications, against a fake Telegram Bot API: the token checked and never shown, pairing and strangers ignored, routing by type, agent and severity, a tool approval decided by button and by a "no, …" reply, a web UI decision editing the chat message, decisions turned off, chatting with the default agent and replying to continue, a goal question answered from the chat, alerts and their hourly cap, one listener per bot, the delivery log, and the MQTT channel publishing the same JSON to a real broker
- Discord, against a fake Discord (REST and Gateway): the token checked and hidden and the invite link, pairing by DM and in a server channel, strangers ignored, a tool approval decided by button (deferred, then answered to the presser) and by reply, the message edited, answers coming back as plain text that pings nobody, chatting with the default agent, a server channel ignoring talk not addressed to the bot, the Message Content intent refused until it's turned on in the portal, and resuming the Gateway after a drop
- goal teams: your agents (delegate_action) and sub-agents (assign_action) working actions in parallel, dependencies, team slots, one action per agent per goal, results landing on actions with no report turns, a failed result blocking its action, waiting on the team, the done check, approval mode, duplicate refusals pointing to your agent, and Stop
- workbenches and tool approvals: allow/ask/block per tool, waiting for a decision, "don't ask again", timeouts, and a real workbench over MCP
- skills: matching, loading, outcomes, versions and rollback, proposals in propose/auto/off modes, diffs and stale warnings
- Agent Skills import and export: the SKILL.md field mapping, render-then-parse giving back the same skill, export then import being unchanged (a lone SKILL.md, a folder with text and binary files, and export-all zips), zips holding many skills with the broken ones reported, Git repositories and links to folders in them, imports always arriving as proposals that run the evals (even in Auto mode), updates as revisions, re-importing an archived skill restoring it, and one-agent imports
- skill upkeep: usage, last use, ratings and stale flags on the list, the thresholds, archive and restore (older rows that say "retired" too), the skill review proposing merges and archiving that are tested and only applied when you approve, skipping skills it doesn't know, and running on its schedule or on Run now
- the learning loop: rating replies, corrections, reflections that propose skills and save lessons, the daily cap, and Run now
- your profile: in every agent's instructions (or not, per agent), the word cap, and suggestions you approve
- the knowledge library: Obsidian notes (frontmatter, tags, links), chunking at headings, uploads, web pages, Git sources and NAS shares (SMB and mounted folders) that only re-read changed files, access per collection, recall and tools, runbooks turned into skill proposals, and the same search on Postgres + pgvector
- long conversations: cutting only between turns, summarising older turns (and folding an old summary into a new one), dropping them when the summary fails, compaction in a real conversation, trimming tool results within a turn (calibrated against the token counts the model reports), the warning when instructions don't fit, and reading the window from Ollama (loaded model, Modelfile, or the default)
- evals: cases from replies and feedback, replaying recorded tool calls, read-only tools running for real only when allowed, Ask/Block tools never running, the judge, and skill changes tested without and with (Auto-mode changes held when they make things worse)

## Layout

```
backend/app/
  models.py          all the tables: providers, agents, tools, threads, messages, jobs, skills,
                     reflections, feedback, profile suggestions, knowledge, eval cases and runs…
  runtime.py         builds agents from rows; turns; delegation; report-back; Stop
  huddles.py         huddles: members, lead, @mentions, per-agent checkpoints, catch-up
  scratchpad.py      scratch pads: per-job notes in Valkey, outside the context window
  nextactions.py     next actions: big requests planned and worked one step at a time
  clock.py           the current date and time every agent is told (AHQ_TIMEZONE)
  worker.py          stream consumer: concurrency, heartbeats, reclaiming, registry
  broker.py          all Valkey access: job streams, results, live-event log, locks, cooldowns, stop flags
  mqtt_bridge.py     MQTT subscriptions -> turn jobs (also routes webhook events)
  webhooks.py        inbound webhooks: sender checks (Gitea HMAC, token), allowed networks, rate
                     limits, secrets encrypted at rest, event topics
  vision.py          look / locate / click_on via a per-agent vision model; questions about attached images
  attachments.py     chat attachments: storage, text, manifests, images to models, workbench transfer
  memory.py          long-term memory: storage, embeddings, hybrid search, capture, tidy-up
  migrate.py         schema migrations on startup (lock, adopt, upgrade, backups) and the
                     `python -m app.migrate` CLI; revisions in migrations/versions/
  auth.py            sign-in: accounts, sessions, API tokens, TOTP, lockout, audit log, CLI
  approvals.py       tool approvals: allow / ask / block per tool, decisions, timeouts
  skills.py          skills: matching, loading, outcomes, versions, proposals
  learning.py        the learning loop: ratings, corrections, reflections, skills from documents
  profile.py         your profile: in every agent's instructions; suggestions you approve
  knowledge.py       knowledge library: sources (upload, web, Git/Obsidian, shares), chunking, hybrid search, recall
  knowledge_shares.py  NAS shares for the library: SMB over the network, or NFS/folders mounted under /shares
  ocr.py               OCR for scanned PDFs (Tesseract), used by the library and chat attachments
  knowledge_proposals.py  documents agents suggest: where they may, near-duplicates, approving into a collection
  evals.py           evals: cases, replayed tool calls, the judge, testing skill changes
  context.py         long conversations: context windows, compaction, trimming, overflow warnings
  thinking.py        thinking levels: per-agent setting -> model settings per provider, support check
  scheduler.py       scheduled tasks: cadence -> cron, time zones, firing due runs exactly once
  schedule_text.py   plain-language schedules ("every weekday at 7am") -> the Scheduler's fields
  subagents.py       sub-agents: modes, limits, duplicate check, grants, estimates, ledger
  notifications.py   notices for you: building them from events, routing rules, delivery, settling
                     every copy when decided, the inbox for chat answers (pairing, decisions,
                     replies, default-agent chat), replies back to the chat, send_alert, listeners
  channels/          one driver per kind of channel: mqtt.py, telegram.py (Bot API, long polling),
                     discord.py (REST, and a Gateway websocket that resumes after drops)
  api/notifications.py  Settings → Notifications (/api/notifications)
  workitems.py       standing goals: the board, WIP limits, pulling from Ready, the goal runner and its
                     guards, the card prompt, goal tools, proposals, teams (dependencies, team slots,
                     results on actions from the lead's agents and sub-agents)
  api/workitems.py   the board API (/api/board, /api/work-items)
  slots.py           provider slots: cross-worker limit on concurrent model requests (Valkey leases)
  api/avatars.py     agent avatar upload / serve (animated GIF + still first frame)
  api/desktop.py     take control of an agent's desktop / hand it back
  resources.py       desktops and workbenches: containers via the Docker API, dedicated/shared, leases
  api/resources.py   Settings → Desktops & workbenches
  api/webhooks.py    webhook management (/api/webhooks) and deliveries (POST /hooks/<name>)
  llm.py             model providers: build an agent's model; list a provider's models
  chatgpt.py         ChatGPT plan providers: Sign in with ChatGPT, stored tokens, cross-worker refresh lock
  grok.py            Grok plan providers: Sign in with xAI (device flow), stored tokens, cross-worker refresh lock
  api/               REST + WebSocket (/ws/threads/{id})
  telemetry.py       OTLP -> your Collector -> Phoenix
frontend/src/app/    app shell (sidebar of conversations), chat, and the Settings pages
                     (agents, model providers, sub-agents, usage, memory, skills, knowledge, learning,
                     evals, profile, tools, events, notifications, system, security), the Requests inbox (tool
                     approvals, goals, skill proposals, profile updates, sub-agent requests), the
                     Board and the Scheduler; icons from Lucide (ISC)
desktop/             webtop image: Chromium + Playwright MCP (:8931) + desktop MCP (:8765)
webreader/           text-only web tools as an MCP server (:8800): feeds, readable pages, SearXNG search
labtools/            lab tools image: integrations composed per instance (:8820; docs/labtools.md)
docs/                labtools.md, lab-alerts.md (senders -> webhooks), schema-migrations.md, auto-deploy.md,
                     examples/homelab-crew.md
deploy/lab/          compose overlay example, inventory and per-instance credentials templates,
                     Zabbix media type script, SSH key folder
mqttpublisher/       outbound MQTT as an MCP server (:8810): agent status and allow-listed publishes
deploy/infra/        Postgres + Valkey compose file (Mosquitto optional, "mqtt" profile)
deploy/caddy/        HTTPS (step-ca ACME) and sign-in-protected desktop viewers
deploy/egress/       the workbenches' egress proxy: per-workbench rules from Valkey, blocked-request log
workbench/           an agent's workbench: Python venv, files, shell, saved scripts (MCP)
deploy/searxng/      a private SearXNG for the web reader's search (JSON on, limiter off)
deploy/autodeploy/   deploy.sh (pull, build, back up, restart, health check), the runner's config and
                     systemd unit; .gitea/workflows/deploy.yaml runs it on push (docs/auto-deploy.md)
examples/            example crew (providers, agents, pools, tools, subscriptions) as importable JSON; homelab-crew.json (a worked example)
```
