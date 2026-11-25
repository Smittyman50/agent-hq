# Deployment

Deploy Agent HQ with Docker: infrastructure, the application, more machines, upgrades, sign-in and HTTPS. Back to the [overview](../README.md).

## Deploy (Docker)

1. **Infrastructure,** once, on one host: `deploy/infra/docker-compose.yml` (Postgres 17 with pgvector, Valkey 8, and optionally Mosquitto 2 via the `mqtt` profile). Keeping Postgres and Valkey together on a host dedicated to Agent HQ while sharing your lab's existing MQTT broker works well: the first two hold Agent HQ's state, and MQTT is how the rest of the lab talks to it.
2. **Application:** `cp .env.example .env`, fill in the URLs and keys, set up HTTPS (next section), then `docker compose --profile images build` (the desktop and workbench images) and `docker compose up -d --build`. This starts:
   - `caddy`: HTTPS on 443 (certificate from your step-ca), with http:// redirected
   - `hq`: API, UI and MQTT bridge (plain HTTP on 8000, for this host only). Its `hq-data` volume holds the knowledge library's uploads and Git clones. NFS shares for the knowledge library are mounted here too, under `/shares` (see [Knowledge library](memory-and-knowledge.md#knowledge-library)). It also reads your step-ca root from `deploy/caddy/root_ca.crt` (`AHQ_EXTRA_CA_FILE`), so Git and web sources on your HTTPS hosts (your Gitea) are trusted.
   - `worker`: pool `default`
   - `worker-desktop`: pool `desktop`, for agents that drive desktops
   - `docker-proxy`: the Docker API `hq` uses to run desktops and workbenches, which you create under **Settings → Desktops & workbenches** (see [Desktops and workbenches](desktops-and-workbenches.md#desktops-and-workbenches))
   - `egress`: the workbenches' proxy, which applies each one's network rules (see [Workbenches](desktops-and-workbenches.md#workbenches-an-agents-own-python-environment))
3. **More machines:** copy `docker-compose.yml` and `.env` to another host and keep only the worker services you want there, with `--pools` set to whatever runs on that machine (`gpu`, `desktop`, `lab-east`…). Workers show up on the **Settings → System** page with their pools and load, and each pool's queue depth appears next to them.
4. **More workers on one host:** `docker compose up -d --scale worker=3`.

**Deploying on every push.** A Gitea Actions workflow can do the upgrade for you whenever `main` changes: pull, build, a `pg_dump` if schema migrations are pending, restart, health check. It runs on a Gitea runner on the server itself and does nothing until you set it up. See [docs/auto-deploy.md](auto-deploy.md).

**Upgrading.** Pull, `docker compose up -d --build`. Each process brings the database schema up to date on startup (Alembic revisions, one process at a time). Before upgrading a Postgres install, `docker compose exec hq python -m app.migrate status` shows the pending revisions; take a `pg_dump` if there are any. Workers on other machines need the new image too: an older one refuses to start against a database a newer version has migrated. See [docs/schema-migrations.md](schema-migrations.md).

**Worker names.** In Docker, each worker is named `<service>-<host>-<last 4 of its container ID>`, for example `worker-shockwave-7090` and `worker-shockwave-1c2e`. You can tell which machine and which replica it is, and scaled replicas never share a name. The host name comes from the host's `/etc/hostname`, which the compose file mounts read-only. Where that file doesn't exist (Docker Desktop), set `AHQ_HOST_NAME` in `.env`. Set `AHQ_WORKER_NAME` for a fixed name, as `worker-desktop` does, but only on a service you won't scale. A recreated container (after `up --build`) gets a new ID and so a new suffix. Its old entry drops off the System page once its heartbeat stops, and any job it left unfinished is picked up as usual.

Valkey settings that matter (already set in the infra compose file):
- `appendonly yes`: queued jobs survive a Valkey restart.
- `maxmemory-policy noeviction`: Valkey never quietly discards stream entries.
- `requirepass`: a password, which goes in `AHQ_REDIS_URL` as `redis://:password@host:6379/0`.

### Sign-in and HTTPS

The UI and API need a sign-in. Agents and workers are unaffected, since they talk to Valkey and Postgres directly.

**Your account**
- **First run:** with no account yet, the API process logs a one-time setup code. Find it with `docker compose logs hq | grep "setup code"`. The first visit to the UI asks for it, so nobody else on your network can claim the instance before you do.
- **Passwords** are stored as Argon2id hashes and need at least 10 characters.
- **Two-factor sign-in** is optional. Turn it on under **Settings → Security** by scanning the QR code with any authenticator app.
- **Sessions** use an HttpOnly cookie that is marked Secure over HTTPS. A session ends after `AHQ_SESSION_IDLE_DAYS` without use (default 7) and after `AHQ_SESSION_MAX_DAYS` in any case (default 30). **Settings → Security** lists your signed-in browsers and can sign any of them out. Changing your password signs out every other browser.
- **Locked out?** `docker compose exec hq python -m app.auth set-password <username>`. The same tool can also `reset-2fa <username>`, `sign-out-all` and `unlock`.

**Protections**
- **Lockout:** 5 failed sign-ins from one address, or 10 for one username, within 15 minutes lock further attempts for 15 minutes.
- **Cross-site requests:** writes and WebSockets that use the cookie are refused when they come from another site.
- **Sign-in log:** **Settings → Security** shows sign-ins, failures, lockouts, sign-outs, and every password, 2FA, session and token change.

**API tokens** are for scripts, Home Assistant or curl. Create one under **Settings → Security**, then send `Authorization: Bearer ahq_…`. A token is shown once and stored only as a hash. It can expire, and you can revoke it. It can use the API but can't change your password, 2FA or tokens.

```bash
curl -H "Authorization: Bearer $AHQ_TOKEN" https://shockwave.home.arpa/api/agents
```

**HTTPS with your step-ca.** The `caddy` service terminates TLS and gets its certificate from step-ca over ACME, renewing it automatically (step-ca certificates last 24 hours by default). One-time setup:
1. **On the CA,** add an ACME provisioner if you don't have one: `step ca provisioner add acme --type ACME`, then restart step-ca.
2. **Copy the root certificate** into the repo: `step ca root deploy/caddy/root_ca.crt`. It's git-ignored.
3. **In `.env`:** set `AHQ_SITE=shockwave.home.arpa` and `ACME_CA=https://<your-ca>/acme/acme/directory`. The provisioner name is part of that path.
4. **Network:** step-ca must be able to reach Shockwave on port 80 for the ACME challenge, and `AHQ_SITE` must resolve to Shockwave in your DNS.
5. **Browsers:** your step-ca root needs to be trusted on the machines you browse from. If you already use step-ca certificates elsewhere, it is.

Plain HTTP on port 8000 is published for Shockwave itself only (`127.0.0.1`). While you're getting HTTPS working, set `AHQ_HTTP_BIND=8000` in `.env` to reach it from your LAN, then remove the setting. If you set `AHQ_PUBLIC_URL`, change it to the https address.

**Desktop viewers are behind the same sign-in.** Their ports are no longer published. Instead, Caddy serves each one at `https://<site>/desktops/desktop-N/` and checks your Agent HQ session first (`forward_auth` to `/api/auth/verify`). Agent HQ sets each desktop's `SUBFOLDER` to match its path and fills in the agent's **Desktop viewer URL** when you give it a desktop.
