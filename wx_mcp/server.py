"""
The weather MCP server: wxcore's station lookup and task-shaped query and
chart tools (wxcore.tools) over MCP, for any MCP client.

Milestone 2: lookup_station, against the Databricks SQL warehouse.

Settings come from the environment (WX_CATALOG and the DATABRICKS_* values,
see wxcore/db.py). Locally, main() loads the repo's .env first; a host sets
them as environment variables instead.

Run locally (from the repo root):
    .venv/bin/python -m wx_mcp.server
Then connect MCP Inspector (npx @modelcontextprotocol/inspector) to
http://127.0.0.1:8000/mcp with transport "Streamable HTTP".
"""

import threading
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from wxcore import db

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HOST = "127.0.0.1"  # local only until hosting is decided
PORT = 8000
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
UNAVAILABLE_MSG = (
    "The weather database isn't reachable right now. Try again in a minute."
)

mcp = MCPServer(
    name="weather",
    instructions="Historical weather observations for US and Canadian stations.",
)

# One warehouse connection, opened on first use. Tools run in worker threads, so calls take turns on it.
_conn = None
_conn_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------


def _connection():
    """The shared warehouse connection, (re)opened when it's missing or closed."""
    global _conn
    if _conn is None or not getattr(_conn, "open", True):
        _conn = db.get_databricks_connection()
    return _conn


def _with_connection(fn, *args):
    """fn(conn, *args) on the shared connection; a warehouse failure becomes a ToolError."""
    with _conn_lock:
        try:
            return fn(_connection(), *args)
        except Exception as exc:
            raise ToolError(UNAVAILABLE_MSG) from exc


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------


@mcp.tool(structured_output=True)
def lookup_station(
    location: str, latitude: float | None = None, longitude: float | None = None
) -> dict[str, Any]:
    """
    Finds the weather station for a place. Call it first: every other tool
    takes the station's ghcn_id. location is any city or town in the world,
    an airport name, or an airport code (e.g. "Dulles", "LGA", "Vienna, VA",
    "Paris, France"); add the state or country when the name is ambiguous.
    latitude and longitude are optional, for a place too small to be known
    by name.

    Returns {ghcn_id, name, city, state} for the station, also with
    nearest_to and miles when the place has no station but this one is in
    it. Otherwise {match: "none", nearby, ask_user}: ask the user ask_user
    word for word before using any station. matched_place says which place
    the name was taken to mean.
    """
    result = _with_connection(db.call_lookup_station, location, latitude, longitude)
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    load_dotenv(ENV_PATH)
    db.catalog()  # fail at startup, not on the first call, if WX_CATALOG is unset
    mcp.run("streamable-http", host=HOST, port=PORT)


if __name__ == "__main__":
    main()
