"""
The resources: the stations covered, and each chart's CSV (held in memory for
the last charts, and read only on request so it costs no tokens until then).
"""

import secrets
import time
from collections import OrderedDict
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from mcp_types import ResourceLink
from wx_mcp.helpers import _warehouse
from wxcore import charts, db

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CSV_KEEP = 50  # recent charts' CSVs held for fetching
CSV_TTL_SECONDS = 3600  # and only for an hour: a link isn't a lasting address
# chart id -> (file name, CSV, when it was made); ids are random and long, so a link can't be guessed
_csvs: OrderedDict[str, tuple[str, str, float]] = OrderedDict()


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------


def _csv_link(prefix: str, n: int, chart: dict) -> ResourceLink:
    """Holds the chart's CSV for fetching on request; the link costs a few tokens, the CSV none until read."""
    name = f"{prefix}.csv" if n == 1 else f"{prefix}_{n}.csv"
    chart_id = secrets.token_urlsafe(16)
    now = time.monotonic()
    _csvs[chart_id] = (name, charts.to_csv(chart), now)
    # oldest first: drop expired ones, then any past the count
    while _csvs and (len(_csvs) > CSV_KEEP or now - next(iter(_csvs.values()))[2] > CSV_TTL_SECONDS):
        _csvs.popitem(last=False)
    return ResourceLink(
        type="resource_link",
        uri=f"chart://{chart_id}/{name}",
        name=name,
        mime_type="text/csv",
        description="The chart's data as CSV; read it only if the user asks for the data",
    )


def chart_csv(chart_id: str, name: str) -> str:
    """A chart's data as CSV, held for an hour (the last 50 charts) while the server runs."""
    held = _csvs.get(chart_id)
    if not held or held[0] != name or time.monotonic() - held[2] > CSV_TTL_SECONDS:
        raise ResourceError(
            f"no CSV {name!r} for chart {chart_id!r}: use the link exactly as given (chart_id is the text "
            "after chart:// up to the next /, name its file name); a CSV lasts an hour, for the 50 most "
            "recent charts, until the server restarts. Run the chart tool again for a new link"
        )
    return held[1]


def stations() -> str:
    """The stations this server covers, one per line as place (code). lookup_station takes the code."""
    try:
        with _warehouse() as conn:
            return "\n".join(db.list_home_locations(conn).values())
    except ToolError as exc:  # the SDK hides any other error type behind a generic message
        raise ResourceError(str(exc)) from None


def register(mcp: MCPServer) -> None:
    mcp.resource("chart://{chart_id}/{name}", mime_type="text/csv")(chart_csv)
    mcp.resource("wx://stations", mime_type="text/plain")(stations)
