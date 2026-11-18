# Everything the backend test suite can use, so nothing in it skips: Valkey (worker tests),
# Postgres 17 + pgvector (the database Agent HQ runs on in production), Tesseract (OCR),
# Chromium + Node (the browser tool server and PGlite tests).
#
# deploy/autodeploy/deploy.sh builds it from the commit being deployed (git archive | docker
# build -) and runs the suite in it twice, on SQLite and on Postgres. By hand, from the
# checkout:
#   docker build -t agent-hq-test -f deploy/test/Dockerfile .
#   docker run --rm -e AHQ_TEST_DB=postgres -e AHQ_TEST_STRICT=1 agent-hq-test
FROM python:3.13

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      valkey-server postgresql-17 postgresql-17-pgvector tesseract-ocr chromium nodejs npm \
 && rm -rf /var/lib/apt/lists/*

# The npm packages some tests fetch at run time (versions pinned in the tests), cached here
# so a deploy doesn't depend on the npm registry being reachable.
RUN npm cache add @playwright/mcp@0.0.84 @electric-sql/pglite@0.5.15 \
      @electric-sql/pglite-socket@0.2.18 @electric-sql/pglite-pgvector@0.0.16

# Dependencies first, from pyproject.toml alone: this layer is reused until they change.
WORKDIR /src/backend
COPY backend/pyproject.toml ./
RUN python -c "import tomllib; p = tomllib.load(open('pyproject.toml', 'rb'))['project']; \
print('\n'.join(p['dependencies'] + p['optional-dependencies']['dev']))" > /tmp/requirements.txt \
 && pip install --no-cache-dir --root-user-action=ignore -r /tmp/requirements.txt

# The source last: only this layer changes from commit to commit.
COPY . /src/
RUN pip install --no-cache-dir --root-user-action=ignore --no-deps -e .

ENV PYTHONDONTWRITEBYTECODE=1
CMD ["pytest", "-q", "-p", "no:warnings", "-p", "no:cacheprovider"]
