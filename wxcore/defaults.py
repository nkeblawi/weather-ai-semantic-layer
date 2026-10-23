"""
Defaults for fields a query leaves out, so a caller can send only what the
question states. apply_defaults fills them in place before validation; it
only adds a missing field and never changes one that's set. A field it
can't default (an unknown metric, a unit with no rule) is left out, for
validate_queries to report.

Rules (the same ones SKILL.md and references/count_group.md teach the
resolver):

- metric and threshold, from a named condition (validation.CONDITIONS, e.g.
  snow_day: SNOW >= 0.5); not for events
- month_filter, from half_year (validation.HALF_YEARS: spring = Jan-Jun,
  fall = Jul-Dec), for a first/last date
- aggregation, for daily queries: count with a condition, else the metric's
  units.natural_aggregation (mean or sum)
- event_day_threshold, for events: EVENT_DAY_THRESHOLDS below
- period_aggregation, for month/year/season/snow_season buckets: the
  metric's natural_aggregation
- start_date / end_date, when both are left out: the normals period for a
  query marked "normal": true; else the whole record (ALL_TIME_START to
  today) for a superlative or ranking, a bucketed or calendar-day query, or
  one with a month_filter. A plain daily mean, sum, or count has no default
  period. Not for series.

A top/bottom limit isn't defaulted here: the SQL functions default it.
"""

from __future__ import annotations

from datetime import date

from wxcore.dates import ALL_TIME_START, NORMAL_END_YEAR, NORMAL_START_YEAR
from wxcore.units import natural_aggregation
from wxcore.validation import CONDITIONS, DATE_AGGREGATIONS, HALF_YEARS

# an event is a run of consecutive rain days or snow days
EVENT_DAY_THRESHOLDS = {
    metric: {"operator": operator, "value": value}
    for metric, operator, value in (CONDITIONS["rain_day"], CONDITIONS["snow_day"])
}
BUCKET_UNITS = {"month", "year", "season", "snow_season"}
SUPERLATIVE_AGGREGATIONS = {"max", "min", "top", "bottom", "latest", "earliest"} | DATE_AGGREGATIONS
WHOLE_RECORD_UNITS = BUCKET_UNITS | {"day_of_year"}


def _fill_dates(q, unit, today: date) -> None:
    if "start_date" in q or "end_date" in q or q.get("aggregation") == "series":
        return
    if q.get("normal") is True:
        q["start_date"] = f"{NORMAL_START_YEAR}-01-01"
        q["end_date"] = f"{NORMAL_END_YEAR}-12-31"
    elif (
        q.get("aggregation") in SUPERLATIVE_AGGREGATIONS
        or unit in WHOLE_RECORD_UNITS
        or "month_filter" in q
    ):
        q["start_date"] = ALL_TIME_START
        q["end_date"] = today.isoformat()


def apply_defaults(queries, today: date | None = None) -> None:
    """Fills each query's missing defaultable fields, in place."""
    today = today or date.today()
    for q in queries or []:
        if not isinstance(q, dict):
            continue
        unit = q.get("unit", "day")

        name = q.get("condition")
        condition = CONDITIONS.get(name) if isinstance(name, str) else None
        if condition and unit != "event":
            cond_metric, operator, value = condition
            q.setdefault("metric", cond_metric)
            q.setdefault("threshold", {"operator": operator, "value": value})
        metric = q.get("metric")

        half = q.get("half_year")
        if isinstance(half, str) and half in HALF_YEARS and "month_filter" not in q:
            q["month_filter"] = list(HALF_YEARS[half])

        if unit == "day" and "aggregation" not in q:
            if condition:
                q["aggregation"] = "count"
            elif aggregation := natural_aggregation(metric):
                q["aggregation"] = aggregation

        if unit == "event" and "event_day_threshold" not in q:
            if threshold := EVENT_DAY_THRESHOLDS.get(metric):
                q["event_day_threshold"] = dict(threshold)

        if (
            unit in BUCKET_UNITS
            and "period_aggregation" not in q
            and q.get("aggregation") not in DATE_AGGREGATIONS | {"series"}
        ):
            if period := natural_aggregation(metric):
                q["period_aggregation"] = period

        _fill_dates(q, unit, today)
