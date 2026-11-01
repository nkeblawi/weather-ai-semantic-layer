"""
The resources: the stations covered, and each chart's CSV (held in memory for
the last charts, and read only on request so it costs no tokens until then).
"""

import secrets
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
_csvs: OrderedDict[str, tuple[str, str]] = OrderedDict()  # chart id -> (file name, CSV)


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------


def _csv_link(prefix: str, n: int, chart: dict) -> ResourceLink:
    """Holds the chart's CSV for fetching on request; the link costs a few tokens, the CSV none until read."""
    name = f"{prefix}.csv" if n == 1 else f"{prefix}_{n}.csv"
    chart_id = secrets.token_hex(4)
    _csvs[chart_id] = (name, charts.to_csv(chart))
    while len(_csvs) > CSV_KEEP:
        _csvs.popitem(last=False)
    return ResourceLink(
        type="resource_link",
        uri=f"chart://{chart_id}/{name}",
        name=name,
        mime_type="text/csv",
        description="The chart's data as CSV; read it only if the user asks for the data",
    )


def chart_csv(chart_id: str, name: str) -> str:
    """A chart's data as CSV, held for the last 50 charts while the server runs."""
    held = _csvs.get(chart_id)
    if not held or held[0] != name:
        raise ResourceError(
            f"no CSV {name!r} for chart {chart_id!r}: chart_id is the 8 characters after chart:// "
            "in the link, name its file name; CSVs last for the 50 most recent charts until the server restarts"
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
