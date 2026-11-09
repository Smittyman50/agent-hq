# An agent's workbench: a persistent Python environment, files and a shell, exposed as an
# MCP server on :8770. One container per agent that needs one (workbench-1, workbench-2…).
#
# Everything the agent makes lives in /work (a volume): its files, its virtual environment
# (/work/.venv, so installed packages persist) and its saved scripts (/work/scripts).
# docker-compose.yml runs it as an unprivileged user, with a read-only root filesystem,
# CPU and memory limits, and no network except PyPI through the egress proxy.
FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential git curl jq ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir "fastmcp>=2" \
 && useradd --create-home --uid 1000 agent \
 && mkdir -p /work && chown agent:agent /work

COPY workbench_mcp.py /opt/workbench/workbench_mcp.py
USER agent
WORKDIR /work
ENV WORKBENCH_DIR=/work PIP_DISABLE_PIP_VERSION_CHECK=1
EXPOSE 8770
CMD ["python", "/opt/workbench/workbench_mcp.py"]
