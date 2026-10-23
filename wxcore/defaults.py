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
- aggregation, for daily queries: count with a condition, else the metric's
  units.natural_aggregation (mean or sum)
- event_day_threshold, for events: EVENT_DAY_THRESHOLDS below
- period_aggregation, for month/year/season/snow_season buckets: the
  metric's natural_aggregation

A top/bottom limit isn't defaulted here: the SQL functions default it.
"""

from __future__ import annotations

from wxcore.units import natural_aggregation
from wxcore.validation import CONDITIONS, DATE_AGGREGATIONS

# an event is a run of consecutive rain days or snow days
EVENT_DAY_THRESHOLDS = {
    metric: {"operator": operator, "value": value}
    for metric, operator, value in (CONDITIONS["rain_day"], CONDITIONS["snow_day"])
}
BUCKET_UNITS = {"month", "year", "season", "snow_season"}


def apply_defaults(queries) -> None:
    """Fills each query's missing defaultable fields, in place."""
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
