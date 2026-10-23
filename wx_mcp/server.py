"""
The weather MCP server: wxcore's station lookup and task-shaped query and
chart tools (wxcore.tools) over MCP, for any MCP client.

Milestone 1 is the skeleton: one hello tool, served over Streamable HTTP.

Run locally (from the repo root):
    .venv/bin/python -m wx_mcp.server
Then connect MCP Inspector (npx @modelcontextprotocol/inspector) to
http://127.0.0.1:8000/mcp with transport "Streamable HTTP".
"""

from mcp.server import MCPServer


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HOST = "127.0.0.1"  # local only until hosting is decided
PORT = 8000

mcp = MCPServer(
    name="weather",
    instructions="Historical weather observations for US and Canadian stations.",
)


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------


@mcp.tool()
def hello(name: str) -> str:
    """Says hello back: a check that the server is reachable."""
    return f"Hello, {name}! The weather server is running."


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    mcp.run("streamable-http", host=HOST, port=PORT)


if __name__ == "__main__":
    main()
