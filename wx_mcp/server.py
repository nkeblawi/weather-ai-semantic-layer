"""
The weather MCP server: lookup_station, the wxcore.tools query tools, and the
chart tools (a PNG of the app's chart, plus a CSV resource fetched on request).

Run from the repo root: .venv/bin/python -m wx_mcp.server
(MCP Inspector: Streamable HTTP, http://127.0.0.1:8000/mcp)
"""

import sys
from pathlib import Path
from dotenv import load_dotenv
from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings
from wx_mcp import prompts, resources
from wx_mcp.access import auth, blocklist, limits
from wx_mcp.access import logger as usage_log
from wx_mcp.access import settings as config
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
- If the user wants °C or metric, convert in your reply: °C = (°F - 32) x 5/9, mm = inches x 25.4. A
  difference (a departure from normal, a gap between stations or periods, a change) and a degree-day total
  convert by x 5/9 only, with no -32: +9°F is +5°C. Convert a °C or metric threshold to °F or inches before
  calling a tool (30°C is 86°F). Charts stay in °F and inches: say so when you show one.
- Chart tools return a PNG: show it to the user, and describe it only from the summaries or rows. Terminal
  clients may not display images: unless you know the user can see it, don't say "the chart above", and give
  the file path if your client saved one. Pass on any notes the result gives (they qualify the chart). A CSV
  link is for the user to download; read it only when they ask for the data.
""".format(
    metrics=", ".join(
        f"{code} {label.lower()}" for code, label in METRIC_LABELS.items()
    )
)

# built and checked in main() (wx_mcp/access/settings.py); the limits read it
SETTINGS: config.Settings | None = None


def build(settings: config.Settings | None = None) -> MCPServer:
    """The server with its tools; with settings.issuer, every request needs a valid sign-in token."""
    sign_in = {}
    if settings and settings.issuer:
        sign_in = {
            "auth": AuthSettings(
                issuer_url=settings.issuer,
                resource_server_url=settings.resource,
                validate_token_resource=False,  # JwtVerifier checks the audience itself
            ),
            "token_verifier": auth.JwtVerifier(settings.issuer, settings.resource),
        }
    server = MCPServer(name="weather", instructions=INSTRUCTIONS, **sign_in)
    for module in (queries, charts, resources, prompts):
        module.register(server)
    return server


mcp = build()  # no sign-in: what the tests and MCP Inspector's no-auth runs use


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    global SETTINGS
    try:
        SETTINGS = config.load()
    except config.SettingsError as e:
        sys.exit(f"weather MCP server not started: {e}")
    try:
        server = build(SETTINGS)
    except auth.AuthError as e:
        sys.exit(f"weather MCP server not started: sign-in: {e}")
    blocklist.configure(SETTINGS.blocklist)
    limits.configure(SETTINGS)
    usage_log.configure()
    print(
        f"weather MCP server: catalog {db.catalog()}, limits {SETTINGS.user_per_minute}/min "
        f"and {SETTINGS.user_per_day}/day per user, {SETTINGS.total_per_day}/day in total, "
        f"blocklist {'on' if SETTINGS.blocklist else 'off'}, sign-in {SETTINGS.issuer or 'off'}"
    )
    try:
        server.run("streamable-http")
    except KeyboardInterrupt:
        pass  # Ctrl-C: uvicorn has already shut down; skip the traceback
    print("weather MCP server stopped")


if __name__ == "__main__":
    main()
