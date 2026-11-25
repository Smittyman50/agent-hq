# Deploying automatically on push

When you push to `main`, a Gitea Actions workflow (`.gitea/workflows/deploy.yaml`) runs on the
server that hosts Agent HQ and redeploys it with `deploy/autodeploy/deploy.sh`:

0. **Test.** Builds a test image from the pushed commit (`deploy/test/Dockerfile`: Valkey,
   Postgres 17 with pgvector, Tesseract, Chromium and Node) and runs the whole test suite in it
   twice at once: on SQLite, and on Postgres with every test on a fresh database of its own
   (about 10 minutes). It runs in **strict mode**: a test that would skip because something it
   needs is missing fails instead, so a deploy can't pass with part of the suite left out. The
   commit goes in as a copy (`git archive`), so the checkout, the running stack and your
   database aren't touched; the image's system packages and Python dependencies are cached
   between deploys. If a test fails, the deploy stops here and nothing has changed.
1. **Update.** Fast-forwards the checkout to the pushed commit. It never resets or throws away
   local changes: if the checkout has commits or edits of its own that are in the way, it stops
   and changes nothing.
2. **Build.** `docker compose build`. When `desktop/`, `workbench/` or `mcp-runner/` changed,
   also `docker compose --profile images build`, which a plain build skips. Agent HQ then
   moves each running workbench and package server to its new image by itself, as soon as
   none of its agents has work queued or running (see
   [Automatic recreate](desktops-and-workbenches.md#automatic-recreate-after-a-rebuild)). Desktops keep
   the old image until you **Recreate** them under **Settings → Desktops & workbenches**;
   the log reminds you.
3. **Back up.** Asks the new image which schema revisions are pending
   (`python -m app.migrate status`). If there are any and the database is Postgres, it takes a
   `pg_dump` first, into `~/agent-hq-backups` (the newest 10 are kept). SQLite databases are
   backed up by Agent HQ itself before it migrates.
4. **Start.** `docker compose up -d`. Agent HQ migrates the schema on startup, as usual.
5. **Check.** Waits for `/api/health`, then checks that no service exited. If something
   fails after the start, the log says how to go back to the previous commit.

Pushes that only change Markdown or `docs/` don't deploy. **Actions → Deploy → Run workflow**
deploys on demand, and rebuilds even if the server is already at the latest commit.

The workflow does nothing until you set it up: it needs a runner on the server and a repository
variable naming the checkout. Without them, a push just shows the job as skipped.

## How it's put together

The runner is [Gitea Runner](https://gitea.com/gitea/runner) (formerly `act_runner`), installed
on the Agent HQ server itself in **host mode**: jobs run directly on that machine, as the
runner's user, with no container around them. That's what lets a job run `git` and
`docker compose` in your checkout. It also means:

- **Whatever a workflow says runs as that user.** Register the runner for this one repository
  (not for your whole Gitea instance), so only this repository's workflows can use it.
- **The runner's user is in the `docker` group**, which is as good as root on that machine.
  Use the account that already deploys Agent HQ by hand; don't add a new privileged one.
- The job uses your checkout's own `git` remote and credentials to fetch, so `git fetch` must
  work in the checkout without prompting.

## Setting it up

These steps assume a Debian or Ubuntu server, the Agent HQ checkout already deployed there by
hand (`.env` filled in, `docker compose up -d --build` working), and Gitea 1.21 or later.

### 1. Turn on Actions in Gitea

- On the Gitea server, Actions must be enabled in `app.ini` (it is by default on recent
  versions):

  ```ini
  [actions]
  ENABLED = true
  ```

  Restart Gitea after changing it.
- In the Agent HQ repository: **Settings → Advanced settings → Enable Repository Actions** (the
  Actions tab appears once it's on).

### 2. Check the deploy user on the server

Pick the user that owns the checkout and already runs the stack. As that user:

```bash
cd /path/to/agent-hq
git fetch origin main           # must not ask for a password
docker compose ps               # must work without sudo
command -v curl flock           # both are needed by deploy.sh
```

- If `git fetch` prompts, give the checkout a non-interactive credential: an SSH deploy key
  (read-only is enough) or an HTTPS token stored with a credential helper.
- If `docker` needs sudo: `sudo usermod -aG docker <user>`, then log out and back in.
- Git needs to be 2.34.1 or later (`git --version`).

### 3. Trust your step-ca root (HTTPS to Gitea)

The runner talks to Gitea over HTTPS and uses the system's certificate store. If Gitea's
certificate comes from your step-ca and the server doesn't trust it yet:

```bash
sudo cp root_ca.crt /usr/local/share/ca-certificates/step-ca-root.crt
sudo update-ca-certificates
curl -sSf https://<your-gitea>/api/healthz    # no certificate error = good
```

(`step ca root root_ca.crt` fetches the root, or reuse `deploy/caddy/root_ca.crt` from the
checkout.)

### 4. Install the runner

Download the Linux binary for your CPU (usually amd64) from
<https://dl.gitea.com/gitea-runner/>, then:

```bash
sudo install -m 755 gitea-runner-*-linux-amd64 /usr/local/bin/gitea-runner
gitea-runner --version
```

### 5. Configure and register it

As the deploy user:

```bash
mkdir -p ~/gitea-runner && cd ~/gitea-runner
cp /path/to/agent-hq/deploy/autodeploy/runner-config.yaml config.yaml
```

That config sets one job at a time, a one-hour job timeout, and the label
`agent-hq-deploy:host`, which is what the workflow's `runs-on` asks for. For every other option,
`gitea-runner config generate` prints a fully commented example.

Get a registration token from the repository: **Settings → Actions → Runners → Create new
runner**. A token from there registers the runner for this repository only. Then:

```bash
gitea-runner -c config.yaml register --no-interactive \
  --instance https://<your-gitea> --token <token> --name <server-name>
```

This writes `.runner` (the registration) next to the config. The labels come from the config
file, so `--labels` isn't needed.

### 6. Run it as a service

```bash
sudo cp /path/to/agent-hq/deploy/autodeploy/gitea-runner.service /etc/systemd/system/
sudo sed -i 's/RUNNER_USER/<user>/g' /etc/systemd/system/gitea-runner.service
sudo systemctl daemon-reload
sudo systemctl enable --now gitea-runner
journalctl -u gitea-runner -f      # should say it's connected and waiting for jobs
```

The runner now shows as **Idle** under **Settings → Actions → Runners**.

### 7. Point the workflow at the checkout

**Settings → Actions → Variables → New variable:** name `AHQ_DEPLOY_DIR`, value the absolute
path of the checkout on the server, for example `/home/<user>/agent-hq`.

### 8. Try it

**Actions → Deploy → Run workflow** on `main`. The job log shows each step: fetch, build, the
schema status, any backup, start, health. After that, every push to `main` deploys by itself.

## Settings

`deploy.sh` reads these environment variables. Set them in the workflow step's `env:` (or
export them when you run it by hand):

| Variable | Default | |
|---|---|---|
| `AHQ_DEPLOY_DIR` | the checkout the script is in | the Agent HQ checkout |
| `AHQ_DEPLOY_BRANCH` | `main` | branch to deploy |
| `AHQ_DEPLOY_FORCE` | `0` | `1` rebuilds even if already at the commit |
| `AHQ_DEPLOY_HEALTH_URL` | `http://127.0.0.1:8000/api/health` | what "up" means |
| `AHQ_DEPLOY_HEALTH_WAIT` | `180` | seconds to wait for it |
| `AHQ_DEPLOY_BACKUP` | `1` | `0` skips the `pg_dump` before migrations |
| `AHQ_DEPLOY_BACKUP_DIR` | `~/agent-hq-backups` | where dumps go |
| `AHQ_DEPLOY_KEEP_BACKUPS` | `10` | dumps kept |
| `AHQ_DEPLOY_PG_IMAGE` | `postgres:17-alpine` | image used for `pg_dump`; at least your server's major version |
| `AHQ_DEPLOY_PRUNE` | `1` | `0` keeps dangling images |
| `AHQ_DEPLOY_TESTS` | `1` | `0` deploys without running the tests first |

The `pg_dump` container runs with host networking, so it reaches your database host the same
way the server does. The database URL is read from `AHQ_DB_URL` in `.env` and handed to the
container through a private env file, never on a command line.

## Deploying by hand

The same script works without Actions:

```bash
cd /path/to/agent-hq
bash deploy/autodeploy/deploy.sh             # the latest origin/main
bash deploy/autodeploy/deploy.sh <commit>    # up to a given commit on main
```

## When a deploy fails

- **Tests failed:** the job log shows which, as pytest prints them. Nothing changed on the
  server. To run the same tests yourself, from the checkout:

  ```bash
  docker build -t agent-hq-test -f deploy/test/Dockerfile .
  docker run --rm -e AHQ_TEST_DB=postgres -e AHQ_TEST_STRICT=1 agent-hq-test   # or AHQ_TEST_DB=sqlite
  docker run --rm -e AHQ_TEST_DB=postgres agent-hq-test pytest -q tests/test_stop.py   # one file
  ```
- **Before the start step** (fetch, build, schema check, backup): the old version is still
  running and nothing was migrated. Fix the cause and run the workflow again.
- **After the start step:** the log prints the previous commit and the command to go back to
  it. If a migration ran, restore the backup first: the older version refuses a newer schema.
- **Can't fast-forward:** the checkout on the server has its own commits or edited files.
  `git status` there shows which; commit, move or discard them yourself, then run it again.
- **Job stays queued:** the runner isn't connected, or its label doesn't match. Check
  `journalctl -u gitea-runner` and that the runner is Idle under Settings → Actions → Runners.
