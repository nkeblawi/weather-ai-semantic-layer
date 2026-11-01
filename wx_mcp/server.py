"""
The weather MCP server: lookup_station, the wxcore.tools query tools, and the
chart tools (a PNG of the app's chart, plus a CSV resource fetched on request).

Run from the repo root: .venv/bin/python -m wx_mcp.server
(MCP Inspector: Streamable HTTP, http://127.0.0.1:8000/mcp)
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from mcp.server import MCPServer
from wx_mcp import prompts, resources
from wx_mcp.tools import charts, queries
from wxcore import db
from wxcore.units import METRIC_LABELS

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Sent to clients at connect, for their model's prompt: how to run a
# conversation over these tools (each tool's own rules are in its description)
INSTRUCTIONS = """\
Past weather observations (temperature, rain, snow, wind) at stations in the US and Canada; no forecasts.
- Start with lookup_station for the place the user named, and pass its ghcn_id to the other tools.
  Never guess a ghcn_id or reuse one from memory.
- When something you need is missing (a place, a period, which measurement), ask the user once for all of
  it. Their reply answers that question: put it together with what they first asked and call the tool,
  rather than asking again or treating the reply as a new question. Don't fill in anything neither
  message says.
- Prefer a period kind ("last_years", "season", ...) over working dates out yourself.
- Name what the results give: a count's matching_days with their dates and values, and an extreme's date.
- Metrics: {metrics}.
- Values and thresholds are in °F and inches. Data runs 1-3 days behind today (non-US stations 2-3).
- Chart tools return a PNG: show it to the user, and describe it only from the summaries. Pass on any
  notes the result gives (they qualify the chart). A CSV link
  is for the user to download; read it only when they ask for the data.
""".format(
    metrics=", ".join(
        f"{code} {label.lower()}" for code, label in METRIC_LABELS.items()
    )
)

# what wxcore.db reads to reach the warehouse; checked at startup, by name only
SETTINGS = (
    "WX_CATALOG",
    "DATABRICKS_SERVER",
    "DATABRICKS_HTTP",
    "DATABRICKS_CLIENT",
    "DATABRICKS_SECRET",
)


mcp = MCPServer(name="weather", instructions=INSTRUCTIONS)
queries.register(mcp)
charts.register(mcp)
resources.register(mcp)
prompts.register(mcp)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    if missing := [name for name in SETTINGS if not os.environ.get(name, "").strip()]:
        sys.exit(
            f"weather MCP server not started: set {', '.join(missing)} in the environment or .env"
        )
    print(f"weather MCP server: catalog {db.catalog()}")
    try:
        mcp.run("streamable-http")
    except KeyboardInterrupt:
        pass  # Ctrl-C: uvicorn has already shut down; skip the traceback
    print("weather MCP server stopped")


if __name__ == "__main__":
    main()
