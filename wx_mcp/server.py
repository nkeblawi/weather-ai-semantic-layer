"""
The weather MCP server: lookup_station, the wxcore.tools query tools, and the
chart tools (a PNG of the app's chart, plus a CSV resource fetched on request).

Run from the repo root: .venv/bin/python -m wx_mcp.server
(MCP Inspector: Streamable HTTP, http://127.0.0.1:8000/mcp)
"""

import logging
import os
import secrets
import sys
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal

import vl_convert
from databricks.sdk.errors import DatabricksError
from databricks.sql import exc as sql_exc
from dotenv import load_dotenv
from mcp.server import MCPServer
from mcp.server.mcpserver import Image
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from mcp_types import ResourceLink, ToolAnnotations
from pydantic import Field

from wxcore import answers, charts, db, series, tools
from wxcore.defaults import apply_defaults
from wxcore.units import METRIC_LABELS
from wxcore.validation import (
    CONDITIONS,
    EVENT_METRICS,
    EVENT_VALUES,
    HALF_YEARS,
    METRICS,
    OPERATORS,
    SEASONS,
)
from wxcore.validation import PLOT_EXPLICIT, coerce_series, validate_queries

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
- Chart tools return a PNG: show it to the user, and describe it only from the summaries. A CSV link
  is for the user to download; read it only when they ask for the data.
""".format(metrics=", ".join(f"{code} {label.lower()}" for code, label in METRIC_LABELS.items()))

mcp = MCPServer(name="weather", instructions=INSTRUCTIONS)
logger = logging.getLogger(__name__)

PNG_WIDTH = 1080  # the app stretches charts to the page; a PNG needs a width
CSV_POINTS = 24  # charts with this many points or more get a CSV (the app's button needs more than 24)
# what wxcore.db reads to reach the warehouse; checked at startup, by name only
SETTINGS = ("WX_CATALOG", "DATABRICKS_SERVER", "DATABRICKS_HTTP", "DATABRICKS_CLIENT", "DATABRICKS_SECRET")
CSV_KEEP = 50  # recent charts' CSVs held for fetching
_csvs: OrderedDict[str, tuple[str, str]] = OrderedDict()  # chart id -> (file name, CSV)

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

Grain = Annotated[
    Literal[tuple(sorted(tools.GRAINS))],
    Field(
        description="Pick one point per day, month, year, season, or snow season (July-June, SNOW only); "
        "leave blank to pick from the span"
    ),
]
AgainstNormal = Annotated[
    Literal[tuple(sorted(tools.AGAINST_NORMAL))],
    Field(
        description="Pick overlay (next to the normal range) or anomaly (minus normal); leave blank for neither"
    ),
]
Baseline = Annotated[
    dict | None,
    Field(
        description='Normals years, e.g. {"start_year": 1981, "end_year": 2010}; leave out for 1991-2020'
    ),
]
ClimatologyBy = Annotated[
    Literal[tuple(tools.CLIMATOLOGY_BY)],
    Field(
        description="Pick one point per calendar month, season, week, or day of the year"
    ),
]
CountPer = Annotated[
    Literal[tuple(sorted(tools.COUNT_PER))],
    Field(
        description="Pick to count per year, season, snow season (July-June), or month; or average by "
        "month_of_year, season_of_year, or week_of_year"
    ),
]
Station2 = Annotated[
    str | None,
    Field(
        description="A second ghcn_id to compare, its own line; leave blank for one station"
    ),
]
Station3 = Annotated[
    str | None,
    Field(
        description="A third ghcn_id to compare, its own line; leave blank for two or fewer"
    ),
]
Metric2 = Annotated[
    Literal[tuple(sorted(METRICS))] | None,
    Field(
        description="A second metric on the same chart, its own line (e.g. TMIN with TMAX); leave blank for one"
    ),
]

Period = Annotated[
    dict | None,
    Field(
        description=(
            "A kind the server resolves to dates (prefer these to working dates out yourself): "
            '{"kind": "absolute_year", "year": 2024}; '
            '{"kind": "absolute_year_range", "start_year": 2000, "end_year": 2020}; '
            '{"kind": "absolute_month", "year": 2022, "month": 12}; '
            '{"kind": "absolute_day", "year": 2026, "month": 1, "day": 25}; '
            '{"kind": "season", "season": "winter|spring|summer|fall"} for the most recently '
            'completed one, or with "year" (the year of its last month: winter 2024 is Dec 2023-Feb 2024); '
            '{"kind": "recent_month", "month": 1} for a month name with no year (the most recent one that has started); '
            '{"kind": "relative_days", "days": 30} for "the last 30 days"; '
            '{"kind": "last_years", "years": 5} for "the last 5 years": the N most recent calendar years '
            'through today, this year included ("a few" is 5, "several" or "recent" is 10; don\'t ask how many); '
            '{"kind": "weekday", "weekday": "friday"} for "last Friday" (the most recent one before today); '
            '{"kind": "snow_season", "year": 2010} for a July-June snow season, by the year it ends in; '
            '{"kind": "all_time"}. '
            'Or explicit {"start_date": "2024-01-01", "end_date": "2024-12-31"}. Leave out for the whole record.'
        )
    ),
]


# ---------------------------------------------------------------------------
# Helper Functions
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
    return ToolAnnotations(title=title, read_only_hint=True, idempotent_hint=True, open_world_hint=False)


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
    if plot_mode:
        coerce_series(queries)
    if problems := validate_queries(queries, stations, plot_mode=plot_mode):
        raise ToolError("; ".join(dict.fromkeys(problems)))
    return stations


def _answer(tool, args: dict) -> dict:
    """tool(**args) as a query, checked and answered like the app does."""
    [query] = _queries(tool, args)
    with _warehouse() as conn:
        stations = _checked(conn, [query])
        result = db.call_answer_query(conn, query)
        # a day count also says when (as the app does): its days, or its most extreme
        if days := db.matching_days(conn, query, result):
            result = {**result, "matching_days": days}
        return answers.build_answer_payload(query, result, stations)


def _png(chart: dict) -> bytes:
    """The app's chart (charts.chart_spec) as a PNG, on white at PNG_WIDTH."""
    spec = charts.chart_spec(chart)
    spec.update(width=PNG_WIDTH, background="white")
    return vl_convert.vegalite_to_png(spec, scale=2)


def _iata(conn, ids: list[str]) -> dict[str, str]:
    """Each station's IATA code from analytics.stations (fill_station_info doesn't read it)."""
    params = {f"id{i}": sid for i, sid in enumerate(ids)}
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT ghcn_id, iata FROM {db.catalog()}.analytics.stations "
            f"WHERE ghcn_id IN ({', '.join(':' + k for k in params)})",
            parameters=params,
        )
        return {ghcn_id: iata for ghcn_id, iata in cur.fetchall() if iata}


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


def _chart(tool, args: dict) -> list:
    """tool(**args)'s series queries, answered and drawn like the app's /plot turn."""
    tool_name = tool.__name__
    # station_2/3 and metric_2 (single fields, so no JSON lists to type) become wxcore's lists
    for name, extra in (
        ("station", ("station_2", "station_3")),
        ("metric", ("metric_2",)),
    ):
        if more := [v for v in (args.pop(k, None) for k in extra) if v]:
            args[name] = [args[name], *more]
    queries = _queries(tool, args)
    with _warehouse() as conn:
        stations = _checked(conn, queries, PLOT_EXPLICIT)
        # e.g. IAD_DCA_chart_over_time: the stations in line order (ghcn_id if no IATA code), then the tool
        ids = list(dict.fromkeys(q["station_id"] for q in queries))
        codes = _iata(conn, ids)
        prefix = "_".join([*(codes.get(i, i) for i in ids), tool_name])
        series_list = []
        for entry in queries:
            base_entry = series.baseline_entry(entry)
            rec_entry = series.records_entry(entry)
            series_list.append(
                series.build_series(
                    entry,
                    db.call_answer_query(conn, entry),
                    db.call_answer_query(conn, base_entry) if base_entry else None,
                    stations,
                    records_result=(
                        db.call_answer_query(conn, rec_entry) if rec_entry else None
                    ),
                )
            )
    drawn = series.group_charts(series_list)
    if not drawn:
        raise ToolError("no data to chart for this station and period")
    out = [Image(data=_png(c), format="png") for c in drawn]
    out.append({"summaries": [series.phrasing_summary(s) for s in series_list]})
    out += [
        _csv_link(prefix, n, c)
        for n, c in enumerate(drawn, 1)
        if sum(len(s["points"]) for s in c["series"]) >= CSV_POINTS
    ]
    return out


# ---------------------------------------------------------------------------
# Basic Tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_read_only("Look up station"))
def lookup_station(
    location: str, latitude: float | None = None, longitude: float | None = None
) -> dict:
    """
    Find the station for a place. Call this first: every other tool takes its ghcn_id. Use only ids a
    tool returned in this conversation (this result, a station picked from its nearby list, or one
    already used for the same place); never one you know or recall yourself.
    - location: a city or town anywhere, an airport name, or a code ("Vienna, VA", "Dulles", "LGA").
      Add the state or country when the name is ambiguous ("Paris, TX").
    - A result with ghcn_id is the station to use. With nearest_to and miles, tell the user which
      station answered for their place.
    - A result with ask_user: ask the user that question word for word, and wait for their answer
      before using any station. If they pick one of the stations offered, use its ghcn_id from nearby
      (no second lookup) and answer the question they first asked.
    - Only look up a place the user named: with none, ask them where instead of passing a stand-in
      ("<UNKNOWN>" gets an error back).
    - matched_place is the place the name was taken to mean; if it isn't the user's, look up again
      with a state or country.
    - latitude and longitude: only for a place too small to be found by name.
    """
    with _warehouse() as conn:
        return db.call_lookup_station(conn, location, latitude, longitude)


@mcp.tool(annotations=_read_only("Summarize days"))
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


@mcp.tool(annotations=_read_only("Extreme days"))
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


@mcp.tool(annotations=_read_only("Count days"))
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
    - No period: the whole record. The result also lists the days with their values (matching_days):
      all of them up to 5, else the 5 most extreme (matching_days_are says which). Name them in your
      answer without being asked.
    e.g. metric TMAX, threshold_operator >=, threshold_value 100.
    """
    return _answer(tools.count_days, locals())


@mcp.tool(annotations=_read_only("Calendar day"))
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


@mcp.tool(annotations=_read_only("First or last date"))
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


@mcp.tool(annotations=_read_only("Storm events"))
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


@mcp.tool(annotations=_read_only("Compare periods"))
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
# Charting Tools
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_read_only("Chart over time"))
def chart_over_time(
    station: str,
    metric: Metric,
    station_2: Station2 = None,
    station_3: Station3 = None,
    metric_2: Metric2 = None,
    grain: Grain | None = None,
    season: Season | None = None,
    stat: PeriodStat | None = None,
    trend: bool = False,
    against_normal: AgainstNormal | None = None,
    baseline: Baseline = None,
    months: list[int] | None = None,
    period: Period = None,
):
    """
    A chart of values over time: "plot July highs since 1990", "chart yearly snowfall", "graph this
    year's temperatures against normal".
    - One point per grain; blank picks from the span (daily up to a year, monthly up to 30 years,
      else yearly). season (with or without grain season) gives one season across years.
    - station_2 / station_3 compare stations, metric_2 adds a metric: one line each (at most 4
      lines: stations x metrics).
    - trend adds a trend line; against_normal overlay or anomaly compares with the 1991-2020
      normals (or baseline).
    - Returns the chart as a PNG: show it to the user. Describe it from summaries, never guessing
      at points. A CSV link, when present, is for the user to fetch; don't read it unless they ask
      for the data.
    e.g. metric TMAX, grain year, trend true.
    """
    return _chart(tools.chart_over_time, locals())


@mcp.tool(annotations=_read_only("Chart typical year"))
def chart_climatology(
    station: str,
    metric: Metric,
    by: ClimatologyBy,
    station_2: Station2 = None,
    station_3: Station3 = None,
    metric_2: Metric2 = None,
    stat: PeriodStat | None = None,
    normal: bool = False,
    months: list[int] | None = None,
    period: Period = None,
):
    """
    A chart of the typical year: "plot average highs by month", "chart normal monthly rainfall",
    "graph the typical temperature through the year".
    - One point per calendar month, season, week, or day of the year (day: 366 smoothed points).
    - Over the whole record, the period, or with normal on the 1991-2020 normals (not both normal
      and a period).
    - station_2 / station_3 compare stations, metric_2 adds a metric: one line each (at most 4
      lines: stations x metrics).
    - Returns the chart as a PNG: show it to the user. Describe it from summaries, never guessing
      at points. A CSV link, when present, is for the user to fetch; don't read it unless they ask
      for the data.
    e.g. metric TMAX, metric_2 TMIN, by month, normal true.
    """
    return _chart(tools.chart_climatology, locals())


@mcp.tool(annotations=_read_only("Chart day counts"))
def chart_count_days(
    station: str,
    per: CountPer,
    condition: Condition | None = None,
    metric: Metric | None = None,
    threshold_operator: Operator | None = None,
    threshold_value: float | None = None,
    season: Season | None = None,
    trend: bool = False,
    against_normal: AgainstNormal | None = None,
    baseline: Baseline = None,
    period: Period = None,
):
    """
    A chart of how many days met a condition or threshold: "plot 90-degree days per year",
    "chart freeze days each winter", "graph snow days by month".
    - Give a condition or a metric with threshold_operator and threshold_value, not both.
      Thresholds are in °F or inches: "90-degree days" is TMAX >= 90.
    - per year, season (with season set), snow_season (July-June), or month counts each period;
      month_of_year, season_of_year, or week_of_year averages the count over the years.
    - trend adds a trend line; against_normal compares with the 1991-2020 normals (or baseline).
    - One station only. No period: the whole record.
    - Returns the chart as a PNG: show it to the user. Describe it from summaries, never guessing
      at points. A CSV link, when present, is for the user to fetch; don't read it unless they ask
      for the data.
    e.g. per year, metric TMAX, threshold_operator >=, threshold_value 90, trend true.
    """
    return _chart(tools.chart_count_days, locals())


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


@mcp.resource("chart://{chart_id}/{name}", mime_type="text/csv")
def chart_csv(chart_id: str, name: str) -> str:
    """A chart's data as CSV, held for the last 50 charts while the server runs."""
    held = _csvs.get(chart_id)
    if not held or held[0] != name:
        raise ResourceError(
            f"no CSV {name!r} for chart {chart_id!r}: chart_id is the 8 characters after chart:// "
            "in the link, name its file name; CSVs last for the 50 most recent charts until the server restarts"
        )
    return held[1]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    if missing := [name for name in SETTINGS if not os.environ.get(name, "").strip()]:
        sys.exit(f"weather MCP server not started: set {', '.join(missing)} in the environment or .env")
    print(f"weather MCP server: catalog {db.catalog()}")
    try:
        mcp.run("streamable-http")
    except KeyboardInterrupt:
        pass  # Ctrl-C: uvicorn has already shut down; skip the traceback
    print("weather MCP server stopped")


if __name__ == "__main__":
    main()
