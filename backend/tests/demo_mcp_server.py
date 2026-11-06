"""Tiny stdio MCP server used by the tests."""

from fastmcp import FastMCP

mcp = FastMCP("demo")


@mcp.tool
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


if __name__ == "__main__":
    mcp.run()
