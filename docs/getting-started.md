# Getting started

Run Agent HQ on one machine for development and try the example crew. Back to the [overview](../README.md).

## Quick start (single machine, dev)

You need Postgres, Valkey and (optionally) MQTT. The simplest route is the infra compose file, which always starts Postgres and Valkey:

```bash
cd deploy/infra
cp .env.example .env                      # set the passwords
docker compose up -d
```

MQTT is up to you:
- **Use your home lab's broker:** give Agent HQ its own user with read access to the topics you'll subscribe to, and set `AHQ_MQTT_URL` to that broker. Nothing else to run.
- **Run the bundled Mosquitto** (handy for dev and testing): it's behind the `mqtt` compose profile.

  ```bash
  docker compose --profile mqtt run --rm mosquitto mosquitto_passwd -c /mosquitto/config/passwd agent-hq
  docker compose --profile mqtt up -d       # or set COMPOSE_PROFILES=mqtt in .env
  ```
- **No MQTT at all:** leave `AHQ_MQTT_URL` empty. Everything except event-triggered agents still works.

Backend (one shell for the API, one for a worker):

```bash
cd backend
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp ../.env.example ../.env                         # point AHQ_* at your infra; export it
uvicorn app.main:app --reload --port 8000          # API + UI + MQTT bridge
python -m app.worker --pools default               # a worker (any number, any machine)
```

To run a worker inside the API process instead of separately, set `AHQ_EMBED_WORKER=1`.

Frontend:

```bash
cd frontend
npm install
npx ng serve        # http://localhost:4200, proxies /api and /ws to :8000
```

On the first visit you'll be asked to create your account with a one-time setup code, which the API process prints to its console (see [Sign-in and HTTPS](deployment.md#sign-in-and-https)).

The UI keeps your conversations in the left sidebar; everything else (agents, model providers, memory, tools, events and system status) is under **Settings** at the bottom of the sidebar. Start on the **Settings → Model providers** page and add your Ollama server (or another provider), then create agents and pick a model for each.

To try the example crew, go to **Settings → Agents → Import** and pick `examples/agents.example.json`. It contains two providers (an Ollama server and OpenAI), the agents with their models and pools, a desktop tool server and an alerts subscription, all as data. Then fix the Ollama URL on the **Settings → Model providers** page and choose models that your server actually has.

Without `AHQ_DB_URL`, the system falls back to SQLite files in `AHQ_DATA_DIR`. That's fine for a single machine, but use Postgres as soon as workers run anywhere else.
