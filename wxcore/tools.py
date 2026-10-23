"""
Task-shaped query tools: each function turns one kind of question's
arguments into a wxcore query, for a caller (the MCP server) to run through
defaults.apply_defaults, validation, and db.call_answer_query like any other.
Each tool exposes only the fields its kind of question needs, so a model
picking tools can't express most invalid or misleading combinations.

A period is resolved here, not by the caller: {"kind": ..., ...} (the
dates.resolve kinds, current_date filled in), {"start_date", "end_date"}, or
None for the whole record where that has a meaning (defaults decides).

Bad arguments raise ToolArgumentError, whose message names the allowed
values. Anything the tool passes through (metric, months, thresholds) is
still checked by validate_queries.
"""

from __future__ import annotations
from datetime import date
from itertools import product
from wxcore.dates import (
    NORMAL_END_YEAR,
    NORMAL_START_YEAR,
    SNOW_SEASON_START_MONTH,
    resolve_date_range,
)
from wxcore.units import natural_aggregation
from wxcore.validation import (
    CONDITIONS,
    HISTOGRAM_UNITS,
    MAX_DAILY_SERIES_DAYS,
    MAX_QUERIES_PER_QUESTION,
)


# ---------------------------------------------------------------------------
# Constants: each tool's choices -> query values
# ---------------------------------------------------------------------------


class ToolArgumentError(ValueError):
    """A tool argument that can't be turned into a query."""


# summarize_days stat -> aggregation
SUMMARY_STATS = {
    "average": "mean",
    "total": "sum",
    "median": "median",
    "stddev": "stddev",
}

# choice -> (aggregation, aggregation when ranked; None: can't be ranked)
ORDERS = {"highest": ("max", "top"), "lowest": ("min", "bottom")}
EVENT_SELECTS = {
    "largest": ("max", "top"),
    "smallest": ("min", "bottom"),
    "latest": ("latest", None),
    "earliest": ("earliest", None),
    "count": ("count", None),
    "average": ("mean", None),
    "total": ("sum", None),
}
PERIOD_SELECTS = {
    "highest": ("max", "top"),
    "lowest": ("min", "bottom"),
    "average": ("mean", None),
    "median": ("median", None),
    "count": ("count", None),
}

# calendar_day stat -> aggregation (normal and odds add their own fields)
CALENDAR_STATS = {
    "highest": "max",
    "lowest": "min",
    "average": "mean",
    "normal": "mean",
    "odds": "mean",
}

# first_last_date which -> aggregation
WHICH_DATES = {"first": "first_date", "last": "last_date"}

# compare_periods period_type (a query unit) and period_stat -> period_aggregation
PERIOD_TYPES = {"month", "year", "season", "snow_season"}
PERIOD_STATS = {"average": "mean", "total": "sum"}

# chart grains: points over time, and climatologies (one point per calendar month, ...)
GRAINS = {"day", "month", "year", "season", "snow_season"}
MONTHLY_GRAIN_YEARS = 30  # longer spans get yearly points when no grain is given
CLIMATOLOGY_BY = {
    "month": "month_of_year",
    "season": "season_of_year",
    "week": "week_of_year",
    "day": "day_of_year",
}
COUNT_PER = (GRAINS - {"day"}) | {f"{k}_of_year" for k in ("month", "season", "week")}
AGAINST_NORMAL = {"overlay", "anomaly"}
CHART_DATES = {"first", "last", "both", "growing_season"}
NORMAL_BASELINE = {"start_year": NORMAL_START_YEAR, "end_year": NORMAL_END_YEAR}


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

# Basic Helpers


def _choice(name, value, allowed):
    """value if it's one of allowed, else ToolArgumentError naming them."""
    if value not in allowed:
        raise ToolArgumentError(
            f"{name} {value!r} not allowed; use one of {', '.join(sorted(allowed))}"
        )
    return value


def period_dates(period, today: date | None = None) -> dict:
    """{start_date, end_date} for a tool's period, or {} for none."""
    if period is None:
        return {}
    if not isinstance(period, dict):
        raise ToolArgumentError(
            "period must be an object: {kind, ...} or {start_date, end_date}"
        )
    if "kind" in period:
        payload = {**period, "current_date": (today or date.today()).isoformat()}
        try:
            return resolve_date_range(payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise ToolArgumentError(
                f"period {period!r} can't be resolved: {exc}"
            ) from exc
    if set(period) == {"start_date", "end_date"}:
        return dict(period)
    raise ToolArgumentError("period needs a kind, or both start_date and end_date")


def _with_period(query: dict, period, months, today) -> dict:
    """query with its period's dates and month filter added."""
    query.update(period_dates(period, today))
    if months is not None:
        query["month_filter"] = months
    return query


def _condition_or_threshold(q, condition, metric, threshold) -> None:
    """Adds a named condition (its metric optional), or a metric + threshold, to q."""
    if (condition is None) == (threshold is None):
        raise ToolArgumentError("give a condition or a threshold (not both)")
    if condition is not None:
        q["condition"] = condition
        if metric is not None:
            q["metric"] = metric
    else:
        if metric is None:
            raise ToolArgumentError("a threshold needs a metric")
        q["metric"], q["threshold"] = metric, threshold


def _date_unit(q) -> str:
    """snow_season for a snowfall date (grouped July-June), else year."""
    metric = q.get("metric", CONDITIONS.get(q.get("condition"), (None,))[0])
    return "snow_season" if metric == "SNOW" else "year"


def _select(name, select, choices, ranked, n) -> dict:
    """The aggregation (and limit) for a choice, single or ranked: n implies ranked."""
    single, ranking = choices[_choice(name, select, choices)]
    ranked = ranked or n is not None
    if ranked and ranking is None:
        rankable = " or ".join(k for k, (_, r) in choices.items() if r)
        raise ToolArgumentError(
            f"{name} {select!r} can't be ranked; rank with {rankable}"
        )
    q = {"aggregation": ranking if ranked else single}
    if n is not None:
        q["limit"] = n
    return q


# Charting Helpers


def _lines(station, metric) -> list[tuple]:
    """(station, metric) for each line: one or a list of each, at most MAX_QUERIES_PER_QUESTION."""
    stations = station if isinstance(station, list) else [station]
    metrics = metric if isinstance(metric, list) else [metric]
    lines = list(product(stations, metrics))
    if not lines or len(lines) > MAX_QUERIES_PER_QUESTION:
        raise ToolArgumentError(
            f"a chart has 1-{MAX_QUERIES_PER_QUESTION} lines (stations x metrics), not {len(lines)}"
        )
    return lines


def _series(station, metric, unit) -> dict:
    """A series query's required fields; metric None for one given by a condition."""
    q = {"station_id": station, "aggregation": "series", "unit": unit}
    if metric is not None:
        q["metric"] = metric
    return q


def _period_value(metric, stat) -> str:
    """period_aggregation for a bucket: stat (average/total), else the metric's usual one."""
    if stat is not None:
        return PERIOD_STATS[_choice("stat", stat, PERIOD_STATS)]
    if aggregation := natural_aggregation(metric):
        return aggregation
    raise ToolArgumentError(
        f"metric {metric!r} has no usual average or total; give a stat"
    )


def _grain(metric, dates) -> str:
    """Points for a span: daily up to a year, monthly up to MONTHLY_GRAIN_YEARS, else yearly (snowfall by snow season)."""
    yearly = "snow_season" if metric == "SNOW" else "year"
    if not dates:
        return yearly
    start, end = (date.fromisoformat(dates[k]) for k in ("start_date", "end_date"))
    if (end - start).days <= MAX_DAILY_SERIES_DAYS:
        return "day"
    return "month" if end.year - start.year + 1 <= MONTHLY_GRAIN_YEARS else yearly


def _baseline(against_normal, baseline) -> dict:
    """display and baseline for a chart against normal (the normals period unless baseline is given)."""
    if against_normal is None:
        if baseline is not None:
            raise ToolArgumentError(
                "a baseline needs against_normal (overlay or anomaly)"
            )
        return {}
    return {
        "display": _choice("against_normal", against_normal, AGAINST_NORMAL),
        "baseline": dict(baseline or NORMAL_BASELINE),
    }


def _to_date(metric, today) -> dict:
    """This year to date; for snowfall, this snow season (from July 1) to date."""
    today = today or date.today()
    year = today.year
    if metric == "SNOW":
        month = SNOW_SEASON_START_MONTH
        year -= today.month < month
    else:
        month = 1
    return {
        "start_date": date(year, month, 1).isoformat(),
        "end_date": today.isoformat(),
    }


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------

# --- Basic Tools ---


# Each basic tool returns a flat query dict


def summarize_days(
    station, metric, stat=None, period=None, months=None, today=None
) -> dict:
    """An average, total, median, or spread of daily values over a period. No stat: the metric's usual one."""
    q = {"station_id": station, "metric": metric}
    if stat is not None:
        q["aggregation"] = SUMMARY_STATS[_choice("stat", stat, SUMMARY_STATS)]
    _with_period(q, period, months, today)
    return q


def extreme_days(
    station, metric, order, ranked=False, n=None, period=None, months=None, today=None
) -> dict:
    """
    The highest or lowest day (ties included); or, ranked, a list of days from
    the highest or lowest: n of them, or the SQL functions' default count.
    """
    q = {"station_id": station, "metric": metric}
    q.update(_select("order", order, ORDERS, ranked, n))
    _with_period(q, period, months, today)
    return q


def count_days(
    station,
    condition=None,
    metric=None,
    threshold=None,
    period=None,
    months=None,
    today=None,
) -> dict:
    """How many days met a named condition, or a metric's threshold."""
    q = {"station_id": station, "aggregation": "count"}
    _condition_or_threshold(q, condition, metric, threshold)
    _with_period(q, period, months, today)
    return q


def calendar_day(
    station,
    date_mmdd,
    stat,
    metric=None,
    condition=None,
    threshold=None,
    period=None,
    today=None,
) -> dict:
    """
    One calendar day (MM-DD) across years: its highest or lowest value on
    record, its average, its normal (the normals period), or the odds of a
    condition.
    """
    agg = CALENDAR_STATS[_choice("stat", stat, CALENDAR_STATS)]
    q = {
        "station_id": station,
        "aggregation": agg,
        "unit": "day_of_year",
        "calendar_day": date_mmdd,
    }
    if stat == "odds":
        q["period_aggregation"] = "any"
        _condition_or_threshold(q, condition, metric, threshold)
    else:
        if metric is None:
            raise ToolArgumentError(f"stat {stat!r} needs a metric")
        q["metric"] = metric
    if stat == "normal":
        q["normal"] = True
    _with_period(q, period, None, today)
    return q


def first_last_date(
    station,
    which,
    condition=None,
    metric=None,
    threshold=None,
    half_year=None,
    period=None,
    today=None,
) -> dict:
    """
    The first or last date each year a condition was met, e.g. the last spring
    freeze. Snowfall is grouped by snow season (July-June), anything else by
    calendar year (half_year picks spring or fall).
    """
    q = {
        "station_id": station,
        "aggregation": WHICH_DATES[_choice("which", which, WHICH_DATES)],
    }
    _condition_or_threshold(q, condition, metric, threshold)
    q["unit"] = _date_unit(q)
    if half_year is not None:
        q["half_year"] = half_year
    _with_period(q, period, None, today)
    return q


def events(
    station,
    metric,
    select,
    measure=None,
    ranked=False,
    n=None,
    size=None,
    day_cutoff=None,
    period=None,
    today=None,
) -> dict:
    """
    Rain or snow events (runs of consecutive rain or snow days): the largest or
    smallest (or a ranking), the latest or earliest, how many, their average
    or total. measure: total (the default) or duration; size filters events
    by that measure; day_cutoff replaces the usual rain/snow-day cutoff.
    """
    q = {
        "station_id": station,
        "metric": metric,
        "unit": "event",
    }
    q.update(_select("select", select, EVENT_SELECTS, ranked, n))
    if measure is not None:
        q["event_value"] = measure
    if size is not None:
        q["threshold"] = size
    if day_cutoff is not None:
        q["event_day_threshold"] = day_cutoff
    _with_period(q, period, None, today)
    return q


def compare_periods(
    station,
    metric,
    period_type,
    select,
    season=None,
    ranked=False,
    n=None,
    threshold=None,
    period_stat=None,
    months=None,
    normal=False,
    period=None,
    today=None,
) -> dict:
    """
    Months, years, seasons, or snow seasons (July-June) compared: the highest or
    lowest (or a ranking), their average or median, or how many met a
    threshold. Each period is averaged or totaled as the metric usually is;
    period_stat overrides that. normal: the normals period.
    """
    q = {
        "station_id": station,
        "metric": metric,
        "unit": _choice("period_type", period_type, PERIOD_TYPES),
    }
    q.update(_select("select", select, PERIOD_SELECTS, ranked, n))
    if season is not None:
        q["season"] = season
    if threshold is not None:
        q["threshold"] = threshold
    if period_stat is not None:
        q["period_aggregation"] = PERIOD_STATS[
            _choice("period_stat", period_stat, PERIOD_STATS)
        ]
    if normal:
        q["normal"] = True
    _with_period(q, period, months, today)
    return q


# --- Charting Tools ---


# Each chart tool returns a list of series queries: one per line on the chart.


def chart_over_time(
    station,
    metric,
    grain=None,
    season=None,
    stat=None,
    trend=False,
    against_normal=None,
    baseline=None,
    months=None,
    period=None,
    today=None,
) -> list[dict]:
    """
    Values over time: one point per day, month, year, season (one season
    across years), or snow season (July-June). No grain: from the span (daily
    up to a year, monthly up to 30 years, else yearly). station and metric
    may be lists, one line each. trend: a trend line. against_normal: overlay
    (next to the normal range) or anomaly (minus normal), against the normals
    period or baseline {start_year, end_year}.
    """
    dates = period_dates(period, today)
    normal = _baseline(against_normal, baseline)
    queries = []
    for st, m in _lines(station, metric):
        if grain is not None:
            unit = _choice("grain", grain, GRAINS)
        elif season is not None:
            unit = "season"
        else:
            unit = _grain(m, dates)
        q = _series(st, m, unit)
        if season is not None:
            q["season"] = season
        if unit != "day":
            q["period_aggregation"] = _period_value(m, stat)
        if trend:
            q["trend"] = True
        q.update(normal)
        q.update(dates)
        if months is not None:
            q["month_filter"] = months
        queries.append(q)
    return queries


def chart_climatology(
    station, metric, by, stat=None, normal=False, months=None, period=None, today=None
) -> list[dict]:
    """
    The typical value by calendar month, season, week, or day of the year
    (366 smoothed points) over the whole record, the period, or the normals
    period (normal). station and metric may be lists, one line each.
    """
    unit = CLIMATOLOGY_BY[_choice("by", by, CLIMATOLOGY_BY)]
    dates = period_dates(period, today)
    if normal:
        if dates:
            raise ToolArgumentError("give normal or a period, not both")
        dates = {
            "start_date": f"{NORMAL_START_YEAR}-01-01",
            "end_date": f"{NORMAL_END_YEAR}-12-31",
        }
    queries = []
    for st, m in _lines(station, metric):
        q = _series(st, m, unit)
        if unit != "day_of_year":
            q["period_aggregation"] = _period_value(m, stat)
        q.update(dates)
        if months is not None:
            q["month_filter"] = months
        queries.append(q)
    return queries


def chart_count_days(
    station,
    per,
    condition=None,
    metric=None,
    threshold=None,
    season=None,
    trend=False,
    against_normal=None,
    baseline=None,
    period=None,
    today=None,
) -> list[dict]:
    """
    Days meeting a named condition or a metric's threshold, counted per year,
    season, snow season (July-June), or month; or averaged by calendar month,
    season, or week (month_of_year, season_of_year, week_of_year).
    """
    q = _series(station, None, _choice("per", per, COUNT_PER))
    q["period_aggregation"] = "count"
    _condition_or_threshold(q, condition, metric, threshold)
    if season is not None:
        q["season"] = season
    if trend:
        q["trend"] = True
    q.update(_baseline(against_normal, baseline))
    _with_period(q, period, None, today)
    queries = [q]
    return queries


def chart_odds(
    station, by, condition=None, metric=None, threshold=None, period=None, today=None
) -> list[dict]:
    """The percent of years with at least one day meeting a condition or threshold, by calendar month, season, week, or day."""
    q = _series(station, None, CLIMATOLOGY_BY[_choice("by", by, CLIMATOLOGY_BY)])
    q["period_aggregation"] = "any"
    _condition_or_threshold(q, condition, metric, threshold)
    _with_period(q, period, None, today)
    queries = [q]
    return queries


def chart_dates_by_year(
    station,
    which,
    condition=None,
    metric=None,
    threshold=None,
    half_year=None,
    trend=False,
    period=None,
    today=None,
) -> list[dict]:
    """
    The first or last date each year a condition was met (snowfall by snow
    season). both: the last spring and first fall dates, two lines.
    growing_season: the days between them (a freeze unless another condition
    or threshold is given).
    """
    _choice("which", which, CHART_DATES)
    if which == "growing_season" and condition is None and threshold is None:
        condition = "freeze"
    if which in ("both", "growing_season"):
        lines = [("last_date", "spring"), ("first_date", "fall")]
    else:
        lines = [(WHICH_DATES[which], half_year)]
    queries = []
    for agg, half in lines:
        q = {"station_id": station, "aggregation": "series"}
        _condition_or_threshold(q, condition, metric, threshold)
        q["unit"] = _date_unit(q)
        q["period_aggregation"] = agg
        if half is not None:
            q["half_year"] = half
        if trend:
            q["trend"] = True
        if which == "growing_season":
            q["display"] = "growing_season"
        _with_period(q, period, None, today)
        queries.append(q)
    return queries


def chart_histogram(
    station,
    metric,
    per=None,
    season=None,
    stat=None,
    months=None,
    period=None,
    today=None,
) -> list[dict]:
    """
    A histogram (distribution) of daily values; or, per year, season, snow
    season, or month, of each period's average or total.
    """
    if per is None:
        if season is not None or stat is not None:
            raise ToolArgumentError(
                "season and stat need per (a histogram of per-period values)"
            )
        q = _series(station, metric, "value")
    else:
        q = _series(station, metric, _choice("per", per, HISTOGRAM_UNITS))
        if season is not None:
            q["season"] = season
        q["period_aggregation"] = _period_value(metric, stat)
        q["display"] = "histogram"
    _with_period(q, period, months, today)
    queries = [q]
    return queries


def chart_climate_graph(station, baseline=None, period=None, today=None) -> list[dict]:
    """
    An NWS-style daily climate graph: daily highs and lows over the normal and
    record ranges, for up to a year (default: this year to date).
    """
    dates = period_dates(period, today) or _to_date(None, today)
    queries = []
    for m in ("TMAX", "TMIN"):
        q = _series(station, m, "day")
        q["baseline"] = dict(baseline or NORMAL_BASELINE)
        q["display"] = "climate"
        q.update(dates)
        queries.append(q)
    return queries


def chart_running_total(
    station, metric, periods=None, against_normal=False, baseline=None, today=None
) -> list[dict]:
    """
    A running total of daily rain or snow through one or more periods of up to
    a year, lined up by day (default: this year, or snow season, to date).
    against_normal adds the normal accumulation.
    """
    periods = periods or [None]
    if len(periods) > MAX_QUERIES_PER_QUESTION:
        raise ToolArgumentError(f"at most {MAX_QUERIES_PER_QUESTION} periods")
    queries = []
    for p in periods:
        q = _series(station, metric, "day")
        if against_normal:
            q["baseline"] = dict(baseline or NORMAL_BASELINE)
        q["display"] = "cumulative"
        q.update(period_dates(p, today) or _to_date(metric, today))
        queries.append(q)
    return queries


def chart_station_difference(
    station,
    other_station,
    metric,
    grain=None,
    season=None,
    stat=None,
    trend=False,
    period=None,
    today=None,
) -> list[dict]:
    """One station's values minus another's over time (grains as in chart_over_time)."""
    queries = chart_over_time(
        [station, other_station],
        metric,
        grain=grain,
        season=season,
        stat=stat,
        trend=trend,
        period=period,
        today=today,
    )
    for q in queries:
        q["display"] = "difference"
    return queries


def chart_climograph(
    station, precip="PRCP", normal=False, period=None, today=None
) -> list[dict]:
    """Monthly average highs and lows with monthly precipitation (or snowfall) totals, as one chart."""
    if precip not in ("PRCP", "SNOW"):
        raise ToolArgumentError("precip must be PRCP or SNOW")
    queries = chart_climatology(
        station,
        ["TMAX", "TMIN", precip],
        "month",
        normal=normal,
        period=period,
        today=today,
    )
    for q in queries:
        q["display"] = "climograph"
    return queries
