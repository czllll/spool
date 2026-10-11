"""Test MCP server built with the official Python SDK (mcp >= 2), for interop tests."""

from mcp.server.mcpserver import MCPServer

server = MCPServer("sdk-calc")


@server.tool()
def multiply(a: int, b: int) -> int:
    """Multiply two integers."""
    return a * b


if __name__ == "__main__":
    server.run("stdio")
