#!/bin/sh
# Serve the package's stdio MCP server over HTTP. The container's command is the package
# command, e.g. npx -y @scope/server@1.2.3 --flag (Agent HQ sets it).
#   MCP_RUNNER_NAME   the server's network name (requests must be addressed to it)
#   MCP_RUNNER_TOKEN  the bearer token Agent HQ sends (kept out of the package's environment)
set -eu
[ "$#" -gt 0 ] || { echo "no package command given" >&2; exit 2; }
name="${MCP_RUNNER_NAME:?MCP_RUNNER_NAME is not set}"
token="${MCP_RUNNER_TOKEN:?MCP_RUNNER_TOKEN is not set}"
unset MCP_RUNNER_TOKEN
exec /opt/mcp-proxy/bin/mcp-proxy --host 0.0.0.0 --port 8790 \
  --allowed-host "$name:8790" --allowed-host "$name" \
  --auth-bearer-token "$token" --pass-environment -- "$@"
