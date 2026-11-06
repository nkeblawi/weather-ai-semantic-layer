"""
What the query and chart tools share: the warehouse connection, turning a
tool's arguments into checked queries, and the data-lag hint.
"""

import logging
from contextlib import contextmanager
from datetime import timedelta
from databricks.sdk.errors import DatabricksError
from databricks.sql import exc as sql_exc
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from wxcore import db, tools
from wxcore.dates import ALL_TIME_START, current_date
from wxcore.defaults import apply_defaults
from wxcore.units import natural_aggregation
from wxcore.validation import TEMP_METRICS, coerce_series, validate_queries

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# hints added to wxcore validation messages that need another tool or a different request
ERROR_HINTS = {
    "wasn't returned by lookup_station": "Call lookup_station with the place name and use the ghcn_id it returns.",
    "start_date is in the future": "This server has past observations only, no forecasts: use a period that has already started.",
}
DATA_LAG_DAYS = 3  # observations run 1-3 days behind today
RECENT_HINT = (
    "the last 1-3 days may not be in yet; say the result is through the latest data"
)
TIES_HINT = "ties pushed more entries into these ranks than the 15 rows shown; say the list is cut off by ties"


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------


@contextmanager
def _warehouse():
    """
    wxcore's warehouse connection, with its failures as plain ToolErrors. The connector already
    retries network errors, so nothing is retried here. The client never sees the raw error (it
    can name the host); the server log gets it in full.
    """
    try:
        with db.get_databricks_connection() as conn:
            yield conn
    except ToolError:
        raise
    except sql_exc.OperationalError as exc:
        logger.warning("warehouse unreachable: %s", exc)
        raise ToolError(
            "the weather warehouse didn't respond (it may be starting up); wait a minute and try once more"
        ) from None
    except (sql_exc.Error, DatabricksError) as exc:
        logger.error("warehouse error: %s", exc)
        raise ToolError(
            "the weather warehouse couldn't run this query; this is a server problem, not your request, "
            "so don't retry it"
        ) from None


def _read_only(title: str) -> ToolAnnotations:
    """Every tool only reads the warehouse: clients can skip confirmation prompts."""
    return ToolAnnotations(
        title=title, read_only_hint=True, idempotent_hint=True, open_world_hint=False
    )


class ToolSet:
    """
    A module's tools: @tool("Title") above each, and register(mcp) adds them to a
    server. They stay at module level because the SDK sends a docstring as it is:
    nested in a function, each line of every description would gain an indent.
    """

    def __init__(self):
        self.tools = []

    def __call__(self, title: str):
        def add(fn):
            self.tools.append((fn, title))
            return fn

        return add

    def register(self, mcp: MCPServer) -> None:
        for fn, title in self.tools:
            mcp.tool(annotations=_read_only(title))(fn)


def _threshold(args: dict, name: str) -> None:
    """Replaces args' <name>_operator and <name>_value with <name> = {"operator", "value"}."""
    operator, value = args.pop(f"{name}_operator"), args.pop(f"{name}_value")
    if (operator is None) != (value is None):
        raise ToolError(f"give both {name}_operator and {name}_value")
    args[name] = {"operator": operator, "value": value} if operator else None


def _queries(tool, args: dict) -> list[dict]:
    """tool(**args) as a list of queries."""
    for name in ("threshold", "size", "day_cutoff"):
        if f"{name}_operator" in args:
            _threshold(args, name)
    try:
        out = tool(**args)
    except tools.ToolArgumentError as exc:
        raise ToolError(str(exc)) from exc
    return out if isinstance(out, list) else [out]


def _checked(conn, queries: list[dict], plot_mode=None) -> dict:
    """Fills, defaults, and validates queries like the app does; returns the station info."""
    stations = {}
    db.fill_station_info(conn, queries, stations)
    apply_defaults(queries)
    for q in queries:
        # valid to wxcore, but adding up daily highs gives a number that means nothing
        if natural_aggregation(q.get("metric")) == "mean" and "sum" in (
            q.get("aggregation"),
            q.get("period_aggregation"),
        ):
            degree_days = (
                "; for accumulated heating or cooling, use HDD or CDD"
                if q["metric"] in TEMP_METRICS
                else ""
            )
            raise ToolError(
                f"a total of {q['metric']} doesn't mean anything: use average, or leave the stat blank{degree_days}"
            )
    if plot_mode:
        coerce_series(queries)
    if problems := validate_queries(queries, stations, plot_mode=plot_mode):
        text = "; ".join(dict.fromkeys(problems))
        hints = [hint for match, hint in ERROR_HINTS.items() if match in text]
        raise ToolError(" ".join([text + ".", *hints]) if hints else text)
    return stations


def _recent(queries: list[dict]) -> bool:
    """
    A period ending within DATA_LAG_DAYS of today (or later), whose last days may not be in yet.
    Not the whole record, where a few days don't matter, nor a month filter that leaves those days out.
    """
    today = current_date()
    start = today - timedelta(days=DATA_LAG_DAYS)
    lag_months = {start.month, today.month}
    return any(
        q.get("end_date", "") >= start.isoformat()
        and q.get("start_date") != ALL_TIME_START
        and (not q.get("month_filter") or lag_months & set(q["month_filter"]))
        for q in queries
    )
