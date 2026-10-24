"""
The weather MCP server: lookup_station and the wxcore.tools query tools.

Run from the repo root: .venv/bin/python -m wx_mcp.server
(MCP Inspector: Streamable HTTP, http://127.0.0.1:8000/mcp)
"""

from pathlib import Path
from typing import Annotated, Literal

from dotenv import load_dotenv
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from wxcore import answers, db, tools
from wxcore.defaults import apply_defaults
from wxcore.validation import (
    CONDITIONS,
    EVENT_METRICS,
    EVENT_VALUES,
    HALF_YEARS,
    METRICS,
    OPERATORS,
    SEASONS,
)
from wxcore.validation import validate_queries

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

mcp = MCPServer(name="weather")

# dropdowns; the values come from wxcore, and client models read the descriptions
Metric = Annotated[
    Literal[tuple(sorted(METRICS))],
    Field(description="Pick a weather metric to measure"),
]
Condition = Annotated[
    Literal[tuple(CONDITIONS)],
    Field(
        description="Pick a kind of day, or leave blank and set metric + threshold instead"
    ),
]
Operator = Annotated[
    Literal[tuple(sorted(OPERATORS))],
    Field(description="Pick how to compare with the value"),
]
SummaryStat = Annotated[
    Literal[tuple(tools.SUMMARY_STATS)],
    Field(
        description="Pick how to combine days, or leave blank for the usual (average; total for rain and snow)"
    ),
]
CalendarStat = Annotated[
    Literal[tuple(tools.CALENDAR_STATS)],
    Field(
        description="Pick record highest/lowest, average, normal (1991-2020), or odds (needs a condition or threshold)"
    ),
]
Order = Annotated[
    Literal[tuple(tools.ORDERS)],
    Field(description="Pick highest or lowest"),
]
Which = Annotated[
    Literal[tuple(tools.WHICH_DATES)],
    Field(description="Pick first or last date each year"),
]
HalfYear = Annotated[
    Literal[tuple(HALF_YEARS)],
    Field(
        description="Pick spring for a last freeze, fall for a first freeze; leave blank for snow"
    ),
]
EventMetric = Annotated[
    Literal[tuple(sorted(EVENT_METRICS))],
    Field(description="Pick PRCP for rain storms or SNOW for snowstorms"),
]
EventSelect = Annotated[
    Literal[tuple(tools.EVENT_SELECTS)],
    Field(description="Pick which events to find, or what to report about them"),
]
Measure = Annotated[
    Literal[tuple(sorted(EVENT_VALUES))],
    Field(description="Pick total amount or duration in days; leave blank for total"),
]
PeriodType = Annotated[
    Literal[tuple(sorted(tools.PERIOD_TYPES))],
    Field(description="Pick what to compare; use snow_season for snowfall by winter"),
]
PeriodSelect = Annotated[
    Literal[tuple(tools.PERIOD_SELECTS)],
    Field(description="Pick which periods to find, or what to report about them"),
]
Season = Annotated[
    Literal[tuple(sorted(SEASONS))],
    Field(description="Pick a season; needed when period_type is season"),
]
PeriodStat = Annotated[
    Literal[tuple(tools.PERIOD_STATS)],
    Field(
        description="Pick average or total per period, or leave blank for the metric's usual"
    ),
]
N = Annotated[
    int | None,
    Field(description="Enter any number between 1 and 15. Default is 3 if blank."),
]

Period = Annotated[
    dict | None,
    Field(
        description='e.g. {"kind": "absolute_year", "year": 2024} or {"start_date": "2024-01-01", '
        '"end_date": "2024-12-31"}; leave out for the whole record'
    ),
]


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------


def _threshold(args: dict, name: str) -> None:
    """Replaces args' <name>_operator and <name>_value with <name> = {"operator", "value"}."""
    operator, value = args.pop(f"{name}_operator"), args.pop(f"{name}_value")
    if (operator is None) != (value is None):
        raise ToolError(f"give both {name}_operator and {name}_value")
    args[name] = {"operator": operator, "value": value} if operator else None


def _answer(tool, args: dict) -> dict:
    """tool(**args) as a query, checked and answered like the app does."""
    for name in ("threshold", "size", "day_cutoff"):
        if f"{name}_operator" in args:
            _threshold(args, name)
    try:
        query = tool(**args)
    except tools.ToolArgumentError as exc:
        raise ToolError(str(exc)) from exc
    with db.get_databricks_connection() as conn:
        stations = {}
        db.fill_station_info(conn, [query], stations)
        apply_defaults([query])
        if problems := validate_queries([query], stations):
            raise ToolError("; ".join(problems))
        return answers.build_answer_payload(
            query, db.call_answer_query(conn, query), stations
        )


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------


@mcp.tool()
def lookup_station(
    location: str, latitude: float | None = None, longitude: float | None = None
) -> dict:
    """
    Find the station for a place. Call this first: every other tool takes its ghcn_id, and you must
    never guess or reuse an id from memory.
    - location: a city or town anywhere, an airport name, or a code ("Vienna, VA", "Dulles", "LGA").
      Add the state or country when the name is ambiguous ("Paris, TX").
    - A result with ghcn_id is the station to use. With nearest_to and miles, tell the user which
      station answered for their place.
    - A result with ask_user: ask the user that question word for word, and wait for their answer
      before using any station.
    - matched_place is the place the name was taken to mean; if it isn't the user's, look up again
      with a state or country.
    - latitude and longitude: only for a place too small to be found by name.
    """
    with db.get_databricks_connection() as conn:
        return db.call_lookup_station(conn, location, latitude, longitude)


@mcp.tool()
def summarize_days(
    station: str,
    metric: Metric,
    stat: SummaryStat | None = None,
    period: Period = None,
    months: list[int] | None = None,
) -> dict:
    """
    One period's average, total, median, or spread (stddev) of daily values: "average high in
    July 2024", "total rain this year", "how much snow fell last winter".
    - Needs a period: the dates the question names.
    - stat blank: the metric's usual one (temperatures averaged; rain and snow totaled).
    - months keeps only those calendar months inside the period.
    - Not for the single hottest or coldest day (extreme_days), counting days (count_days), or a
      typical month across years ("average January high": compare_periods).
    - Answer from the result's value, units, and computed (what was calculated).
    e.g. metric TMAX, period {"kind": "absolute_month", "year": 2024, "month": 7}.
    """
    return _answer(tools.summarize_days, locals())


@mcp.tool()
def extreme_days(
    station: str,
    metric: Metric,
    order: Order,
    ranked: bool = False,
    n: N = None,
    period: Period = None,
    months: list[int] | None = None,
) -> dict:
    """
    The single highest or lowest day, or a ranking of days: "hottest day on record", "coldest
    night this winter", "5 snowiest days".
    - order highest or lowest; coldest nights are TMIN lowest.
    - The single day includes ties; ranked or n gives a list instead (n 1-15, blank 3).
    - months for a month across years ("hottest January day ever": months [1]).
    - No period: the whole record.
    - Not for storms over several days (events), or the hottest summer or snowiest winter
      (compare_periods).
    e.g. metric TMAX, order highest.
    """
    return _answer(tools.extreme_days, locals())


@mcp.tool()
def count_days(
    station: str,
    condition: Condition | None = None,
    metric: Metric | None = None,
    threshold_operator: Operator | None = None,
    threshold_value: float | None = None,
    period: Period = None,
    months: list[int] | None = None,
) -> dict:
    """
    How many days met a condition or threshold: "days over 100°F", "freeze days last winter",
    "has it ever hit 80 in January" (months [1]; 0 means never).
    - Give a condition (freeze is a low of 32°F or below) or a metric with threshold_operator and
      threshold_value, not both.
    - Thresholds are in °F or inches: "90-degree days" is TMAX >= 90, "nights below 20" is TMIN < 20.
    - No period: the whole record. A small count also lists the days.
    e.g. metric TMAX, threshold_operator >=, threshold_value 100.
    """
    return _answer(tools.count_days, locals())


@mcp.tool()
def calendar_day(
    station: str,
    date_mmdd: str,
    stat: CalendarStat,
    metric: Metric | None = None,
    condition: Condition | None = None,
    threshold_operator: Operator | None = None,
    threshold_value: float | None = None,
    period: Period = None,
) -> dict:
    """
    One calendar date across all years: "record high on July 4", "normal low on Christmas",
    "odds of a white Christmas".
    - date_mmdd is MM-DD with leading zeros ("07-04").
    - highest and lowest give the record and its year; average and normal (1991-2020) are smoothed
      over nearby days; these need a metric.
    - odds is the percent of years the condition happened on that date; it needs a condition or a
      threshold, not a metric alone.
    e.g. date_mmdd 12-25, stat odds, condition snow_on_ground.
    """
    return _answer(tools.calendar_day, locals())


@mcp.tool()
def first_last_date(
    station: str,
    which: Which,
    condition: Condition | None = None,
    metric: Metric | None = None,
    threshold_operator: Operator | None = None,
    threshold_value: float | None = None,
    half_year: HalfYear | None = None,
    period: Period = None,
) -> dict:
    """
    The first or last date each year something happened: "last spring freeze", "first 90-degree
    day", "first measurable snow".
    - Freezes: half_year spring for the last freeze, fall for the first.
    - Snowfall is grouped by snow season (July-June): leave half_year blank.
    - A one-year period gives that year's date; a longer one (or none: the whole record) gives the
      typical date and the earliest and latest years.
    e.g. which last, condition freeze, half_year spring.
    """
    return _answer(tools.first_last_date, locals())


@mcp.tool()
def events(
    station: str,
    metric: EventMetric,
    select: EventSelect,
    measure: Measure | None = None,
    ranked: bool = False,
    n: N = None,
    size_operator: Operator | None = None,
    size_value: float | None = None,
    day_cutoff_operator: Operator | None = None,
    day_cutoff_value: float | None = None,
    period: Period = None,
) -> dict:
    """
    Rain or snow storms: runs of consecutive days that each had rain (0.05+ in.) or snow (0.5+ in.).
    "biggest snowstorm", "storms over 6 inches this winter", "longest rainy spell".
    - select largest/smallest by the storm's total (or its length in days with measure duration).
    - latest/earliest is the most recent or first storm by date, not the biggest; add size to
      require a minimum ("last storm over 6 inches": size_operator >, size_value 6).
    - The snowiest single day is extreme_days, not this.
    - No period: the whole record.
    e.g. metric SNOW, select largest, n 5.
    """
    return _answer(tools.events, locals())


@mcp.tool()
def compare_periods(
    station: str,
    metric: Metric,
    period_type: PeriodType,
    select: PeriodSelect,
    season: Season | None = None,
    ranked: bool = False,
    n: N = None,
    threshold_operator: Operator | None = None,
    threshold_value: float | None = None,
    period_stat: PeriodStat | None = None,
    months: list[int] | None = None,
    normal: bool = False,
    period: Period = None,
) -> dict:
    """
    Whole months, years, seasons, or winters compared with each other: "snowiest winter",
    "hottest summers", "average January high", "normal July high".
    - period_type: snow_season (July-June) for snowfall by winter, never season; season (with
      season set) for other metrics; month or year otherwise.
    - Each period is averaged or totaled as the metric usually is (period_stat overrides).
    - select count with a threshold counts periods meeting it ("winters with over 30 inches").
    - normal on: the 1991-2020 normals period ("normal July high": month, average, months [7]).
    - Periods under 90% observed are left out; the result says which were counted.
    - No period: the whole record.
    e.g. metric SNOW, period_type snow_season, select highest.
    """
    return _answer(tools.compare_periods, locals())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    mcp.run("streamable-http")


if __name__ == "__main__":
    main()
