# Package servers: an MCP server published as an npm or PyPI package (Settings -> Tools ->
# Catalog -> Install), run in a container of its own. One container per package server.
#
# The package speaks MCP over stdio; mcp-proxy turns that into streamable HTTP on :8790
# (/mcp), and only answers requests that carry the server's own bearer token and are
# addressed to its name. Agent HQ runs it like a workbench: an unprivileged user, a
# read-only root filesystem, CPU and memory limits, and no network except the egress
# proxy (PyPI by default, plus the npm registry and the hosts you allow on its page).
#
# The package and its caches live in /work (a volume), so it is downloaded once.
FROM node:22-slim AS node

FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && apt-get update && apt-get install -y --no-install-recommends ca-certificates git libstdc++6 \
 && rm -rf /var/lib/apt/lists/* \
 # mcp-proxy in its own venv, so the package's own dependencies can't clash with it
 && uv venv /opt/mcp-proxy && uv pip install --no-cache --python /opt/mcp-proxy/bin/python "mcp-proxy==0.15.*" \
 && useradd --create-home --uid 1000 agent \
 && mkdir -p /work && chown agent:agent /work

COPY run.sh /opt/runner/run.sh
# Executable even when the repo was checked out on Windows (no exec bit there)
RUN chmod 755 /opt/runner/run.sh
USER agent
WORKDIR /work
# Caches in /work; uvx uses this image's Python rather than downloading one; Node's fetch
# goes through HTTPS_PROXY (the egress proxy) like npm, pip and uv do.
ENV HOME=/work npm_config_cache=/work/.npm npm_config_update_notifier=false \
    UV_CACHE_DIR=/work/.cache/uv UV_TOOL_DIR=/work/.uv-tools UV_PYTHON_DOWNLOADS=never \
    NODE_USE_ENV_PROXY=1 NODE_NO_WARNINGS=1 PYTHONUNBUFFERED=1
EXPOSE 8790
ENTRYPOINT ["/opt/runner/run.sh"]
