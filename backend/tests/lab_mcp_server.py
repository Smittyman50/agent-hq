"""A tiny 'homelab' MCP server for the eval tests: one read-only tool, one that changes
things (and leaves a marker file, so tests can prove it never ran during an eval)."""

import os
from pathlib import Path

from fastmcp import FastMCP

mcp = FastMCP("lab")
MARKS = Path(os.environ["LAB_DIR"])


@mcp.tool(annotations={"readOnlyHint": True})
def list_vms() -> list[str]:
    """List the virtual machines."""
    (MARKS / "listed").write_text("x")
    return ["web", "db"]


@mcp.tool
def restart_vm(name: str) -> str:
    """Restart a virtual machine."""
    (MARKS / f"restarted-{name}").write_text("x")
    return f"{name} restarted"


if __name__ == "__main__":
    mcp.run()
