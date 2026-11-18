#!/usr/bin/env bash
# Pull, build and restart Agent HQ on this host.
#
# Run by the Gitea Actions workflow (.gitea/workflows/deploy.yaml) on a runner on the
# same machine, or by hand from the checkout:
#
#   bash deploy/autodeploy/deploy.sh            # deploy the latest origin/main
#   bash deploy/autodeploy/deploy.sh <commit>   # deploy up to a commit on origin/main
#
# What it does, in order (it stops at the first problem and changes nothing after it):
#   0. runs the test suite on the commit, on SQLite and on Postgres, in a test image built
#      from it (deploy/test/Dockerfile); if a test fails, it stops here, before the
#      checkout or anything running has changed
#   1. fast-forwards the checkout to the commit (never resets or discards local changes)
#   2. builds the images, and the desktop/workbench/package-server images when their
#      folders changed
#   3. asks the new image which schema revisions are pending; if there are any and the
#      database is Postgres, takes a pg_dump first (SQLite is backed up by Agent HQ itself)
#   4. starts the new version (docker compose up -d); Agent HQ migrates on startup
#   5. waits for /api/health and checks that no service exited
#
# Settings (environment variables; all optional when run from the checkout):
#   AHQ_DEPLOY_DIR          the Agent HQ checkout (default: the one this script is in)
#   AHQ_DEPLOY_BRANCH       branch to deploy (main)
#   AHQ_DEPLOY_FORCE        1 = rebuild even if the checkout is already at the commit
#   AHQ_DEPLOY_HEALTH_URL   http://127.0.0.1:8000/api/health
#   AHQ_DEPLOY_HEALTH_WAIT  seconds to wait for it (180)
#   AHQ_DEPLOY_BACKUP       0 = no pg_dump before migrations (1)
#   AHQ_DEPLOY_BACKUP_DIR   where dumps go ($HOME/agent-hq-backups)
#   AHQ_DEPLOY_KEEP_BACKUPS how many dumps to keep (10)
#   AHQ_DEPLOY_PG_IMAGE     image with pg_dump, at least your server's version (postgres:17-alpine)
#   AHQ_DEPLOY_PRUNE        0 = keep dangling images (1)
#   AHQ_DEPLOY_TESTS        0 = deploy without running the tests first (1)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${AHQ_DEPLOY_DIR:-$(cd "$here/../.." && pwd)}"
BRANCH="${AHQ_DEPLOY_BRANCH:-main}"
TARGET="${1:-}"
FORCE="${AHQ_DEPLOY_FORCE:-0}"
HEALTH_URL="${AHQ_DEPLOY_HEALTH_URL:-http://127.0.0.1:8000/api/health}"
HEALTH_WAIT="${AHQ_DEPLOY_HEALTH_WAIT:-180}"
BACKUP="${AHQ_DEPLOY_BACKUP:-1}"
BACKUP_DIR="${AHQ_DEPLOY_BACKUP_DIR:-$HOME/agent-hq-backups}"
KEEP_BACKUPS="${AHQ_DEPLOY_KEEP_BACKUPS:-10}"
PG_IMAGE="${AHQ_DEPLOY_PG_IMAGE:-postgres:17-alpine}"
PRUNE="${AHQ_DEPLOY_PRUNE:-1}"
TESTS="${AHQ_DEPLOY_TESTS:-1}"
TEST_IMAGE="agent-hq-test"

started=0  # set once the new version is being started: from then on, failures say how to go back
before=""

log() { printf '\n==> %s\n' "$*"; }
rollback_hint() {
  [ "$started" = 1 ] && [ -n "$before" ] || return 0
  printf '\nThe checkout was at %s before this deploy. To go back to it:\n' "${before:0:12}" >&2
  printf '  cd %q && git checkout %s && docker compose up -d --build\n' "$APP_DIR" "${before:0:12}" >&2
  printf 'If a schema migration ran, restore the backup first (%s); the old version refuses a newer schema.\n' "$BACKUP_DIR" >&2
  printf 'Then git checkout %s again before the next deploy.\n' "$BRANCH" >&2
}
die() { printf '\nDEPLOY FAILED: %s\n' "$*" >&2; rollback_hint; exit 1; }

# The whole test suite, on the commit being deployed, before anything changes. A test image
# (deploy/test/Dockerfile: Valkey, Postgres 17 + pgvector, Tesseract, Chromium, Node) is
# built from a copy of the commit (git archive, so the checkout is never touched; the layers
# with system packages and Python dependencies are cached between deploys). The suite then
# runs twice at once, on SQLite and on Postgres (every test on a fresh database of its own),
# in strict mode: a test that skips because something it needs is missing fails instead.
run_tests() {
  local commit tmp rc=0 db
  commit="$(git rev-parse --short "$1")"
  log "Testing $(git log -1 --format='%h %s' "$1") before deploying it (about 10 minutes)"
  if ! git archive --format=tar "$1" | docker build -q -t "$TEST_IMAGE" -f deploy/test/Dockerfile - >/dev/null; then
    die "couldn't build the test image for $commit, so nothing was deployed (the checkout and the running version are unchanged)."
  fi
  tmp="$(mktemp -d)"
  declare -A pid=()
  for db in sqlite postgres; do
    docker run --rm -e AHQ_TEST_DB="$db" -e AHQ_TEST_STRICT=1 "$TEST_IMAGE" >"$tmp/$db.log" 2>&1 &
    pid[$db]=$!
  done
  for db in sqlite postgres; do
    if wait "${pid[$db]}"; then
      printf '\n--- tests on %s: %s\n' "$db" "$(tail -n 1 "$tmp/$db.log")"
    else
      rc=1
      printf '\n--- tests on %s FAILED ---\n' "$db"
      cat "$tmp/$db.log"
    fi
  done
  rm -rf "$tmp"
  if [ "$rc" != 0 ]; then
    die "the tests failed on $commit (see above), so nothing was deployed: the checkout and the running version are unchanged. Fix and push again (AHQ_DEPLOY_TESTS=0 skips the tests)."
  fi
}
trap 'printf "\nDEPLOY FAILED: a command failed (line %s)\n" "$LINENO" >&2; rollback_hint' ERR

command -v docker >/dev/null || die "docker isn't on the PATH for $(id -un)"
docker compose version >/dev/null 2>&1 || die "docker compose (v2) isn't available to $(id -un)"
command -v curl >/dev/null || die "curl isn't installed (needed for the health check)"
cd "$APP_DIR" 2>/dev/null || die "no Agent HQ checkout at $APP_DIR (set AHQ_DEPLOY_DIR)"
[ -f docker-compose.yml ] && [ -d backend/app ] || die "$APP_DIR doesn't look like an Agent HQ checkout"
[ -f .env ] || die "$APP_DIR/.env is missing: cp .env.example .env and fill it in first"

# One deploy at a time on this host (two quick pushes queue up instead of racing).
exec 9>"${TMPDIR:-/tmp}/agent-hq-deploy.lock"
if ! flock -n 9; then
  log "Another deploy is running; waiting for it to finish"
  flock 9
fi

# ---------------------------------------------------------------------------- 1. update
branch="$(git symbolic-ref --short -q HEAD || true)"
[ "$branch" = "$BRANCH" ] || die "the checkout is on '${branch:-a detached HEAD}', not $BRANCH"
before="$(git rev-parse HEAD)"
log "Fetching origin/$BRANCH"
git fetch --quiet --prune origin "$BRANCH"
target="${TARGET:-origin/$BRANCH}"
git cat-file -e "$target^{commit}" 2>/dev/null || die "commit $target isn't in origin/$BRANCH"
git merge-base --is-ancestor "$target" "origin/$BRANCH" || die "$target isn't on origin/$BRANCH"

if git merge-base --is-ancestor "$target" HEAD; then
  if [ "$FORCE" != 1 ]; then
    log "Already at $(git log -1 --format='%h %s'); nothing to deploy (AHQ_DEPLOY_FORCE=1 rebuilds anyway)"
    exit 0
  fi
  log "Already at $target; rebuilding anyway (AHQ_DEPLOY_FORCE=1)"
  if [ "$TESTS" = 1 ]; then run_tests "$target"; fi
else
  if [ "$TESTS" = 1 ]; then run_tests "$target"; else log "Skipping the tests (AHQ_DEPLOY_TESTS=0)"; fi
  if ! git merge --ff-only --quiet "$target"; then
    git status --short
    die "can't fast-forward to $target: the checkout has its own commits or changes to the files above. Nothing was changed."
  fi
fi
after="$(git rev-parse HEAD)"
log "Deploying $(git log -1 --format='%h %s')"
if [ "$before" != "$after" ]; then
  git --no-pager log --oneline "$before..$after"
fi

# ---------------------------------------------------------------------------- 2. build
log "Building images"
docker compose build

# The desktop, workbench and package-server images (compose profile "images") aren't part
# of the running stack, so a plain build skips them. Rebuild them when their folders
# changed; existing desktops and workbenches keep the old image until you Recreate them.
images_changed=0
if [ "$FORCE" = 1 ] || ! git diff --quiet "$before" "$after" -- desktop workbench mcp-runner; then
  log "Building the desktop, workbench and package-server images"
  docker compose --profile images build
  images_changed=1
fi

# ---------------------------------------------------------------------------- 3. schema
log "Checking the database schema with the new image"
if ! status="$(docker compose run --rm --no-deps -T hq python -m app.migrate status 2>&1)"; then
  printf '%s\n' "$status"
  die "couldn't read the schema status (is the database reachable from this host?). The old version is still running."
fi
printf '%s\n' "$status"
pending="$(grep -c '^pending:' <<<"$status" || true)"

if [ "${pending:-0}" -gt 0 ] && [ "$BACKUP" = 1 ]; then
  db_url="$(sed -n 's/^AHQ_DB_URL=//p' .env | tail -n 1 | sed -e 's/^["'\'']//' -e 's/["'\'']$//' -e 's/[[:space:]]*$//')"
  case "$db_url" in
    postgres://*|postgresql://*|postgresql+*://*)
      mkdir -p "$BACKUP_DIR"
      chmod 700 "$BACKUP_DIR"
      file="$BACKUP_DIR/agent_hq-$(date +%Y%m%d-%H%M%S)-${after:0:7}.sql.gz"
      log "$pending schema revision(s) pending: backing up Postgres to $file"
      # pg_dump wants a plain postgresql:// URL (no SQLAlchemy driver suffix). The URL goes
      # through a private env file, so the password isn't on any command line.
      envfile="$(mktemp)"
      chmod 600 "$envfile"
      printf 'PGURL=%s\n' "$(sed -E 's#^postgres(ql)?(\+[a-z0-9]+)?://#postgresql://#' <<<"$db_url")" >"$envfile"
      if ! docker run --rm --network host --env-file "$envfile" "$PG_IMAGE" \
             sh -c 'pg_dump --no-owner --no-privileges "$PGURL"' | gzip >"$file.part"; then
        rm -f "$file.part" "$envfile"
        die "the backup failed, so nothing was migrated. The old version is still running (fix it, or AHQ_DEPLOY_BACKUP=0 to skip)."
      fi
      rm -f "$envfile"
      mv "$file.part" "$file"
      echo "backup: $(du -h "$file" | cut -f1) $file"
      # Keep the newest KEEP_BACKUPS dumps.
      ls -1t "$BACKUP_DIR"/agent_hq-*.sql.gz 2>/dev/null | tail -n +"$((KEEP_BACKUPS + 1))" | xargs -r rm -f --
      ;;
    "")
      log "$pending schema revision(s) pending; SQLite is backed up by Agent HQ before it migrates" ;;
    *)
      log "$pending schema revision(s) pending; not a Postgres URL, so no pg_dump" ;;
  esac
elif [ "${pending:-0}" -gt 0 ]; then
  log "$pending schema revision(s) pending; no backup (AHQ_DEPLOY_BACKUP=0)"
fi

# ---------------------------------------------------------------------------- 4. start
log "Starting the new version"
started=1
docker compose up -d

# ---------------------------------------------------------------------------- 5. check
log "Waiting for Agent HQ at $HEALTH_URL (up to ${HEALTH_WAIT}s)"
deadline=$((SECONDS + HEALTH_WAIT))
until curl -fsS --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; do
  if [ "$SECONDS" -ge "$deadline" ]; then
    docker compose ps
    docker compose logs --tail 80 hq
    die "Agent HQ didn't answer within ${HEALTH_WAIT}s (its log is above)"
  fi
  sleep 3
done
echo "healthy"

# Workers and tool servers start alongside; give them a moment, then look for any that
# stopped (an older worker refusing a newer schema exits, for example).
sleep 10
down="$(docker compose ps -a --format '{{.Service}} {{.State}}' | awk '$2 != "running" {print $1 " (" $2 ")"}')"
if [ -n "$down" ]; then
  docker compose ps -a
  for svc in $(awk '{print $1}' <<<"$down"); do
    printf '\n--- %s ---\n' "$svc"
    docker compose logs --tail 30 "$svc" || true
  done
  die "Agent HQ is up, but these services aren't running: $(tr '\n' ' ' <<<"$down")"
fi

if [ "$PRUNE" = 1 ]; then
  docker image prune -f >/dev/null
fi
log "Deployed $(git rev-parse --short HEAD) in ${SECONDS}s"
if [ "$images_changed" = 1 ]; then
  echo "The desktop/workbench images were rebuilt. Running desktops and workbenches keep the old"
  echo "image until you Recreate them (Settings -> Desktops & workbenches; their files are kept)."
fi
