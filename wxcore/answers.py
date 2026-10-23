"""
Answer shaping: one query's parameters + its answer_query result as the
plain facts an answer is phrased from, and top/bottom results as a display
table or a ranking chart.
"""

import calendar
from datetime import date

from wxcore.dates import (
    ALL_TIME_START,
    NORMAL_END_YEAR,
    NORMAL_SMOOTHING_DAYS,
    NORMAL_START_YEAR,
    SEASON_MONTHS,
    calendar_day_label,
    season_label,
)
from wxcore.db import station_place
from wxcore.units import (
    COUNT_UNITS,
    METRIC_LABELS,
    METRIC_UNITS,
    TEMP_METRICS,
    answer_units,
    convert_value,
    natural_aggregation,
    threshold_label,
)
from wxcore.validation import MAX_RANK_LIMIT, MIN_BUCKET_COVERAGE, RANKING_AGGREGATIONS


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# What was computed (describe): bucket unit -> (singular, plural)
BUCKET_NAMES = {
    "month": ("month", "months"),
    "year": ("calendar year", "calendar years"),
    "snow_season": ("snow season", "snow seasons"),
}
SNOW_SEASON_NOTE = " (snow seasons run July-June)"
DAILY_STATS = {
    "mean": "Average daily {m}",
    "median": "Median daily {m}",
    "sum": "Total {m}",
    "stddev": "Standard deviation of daily {m}",
    "max": "Highest daily {m}",
    "min": "Lowest daily {m}",
    "top": "Days ranked by {m}, highest first",
    "bottom": "Days ranked by {m}, lowest first",
}
ACROSS_BUCKETS = {
    "mean": "Average of each {one}'s {v}",
    "median": "Median of each {one}'s {v}",
    "sum": "Sum of each {one}'s {v}",
    "stddev": "Standard deviation of each {one}'s {v}",
    "max": "The {one} with the highest {v}",
    "min": "The {one} with the lowest {v}",
    "top": "{Many} ranked by {v}, highest first",
    "bottom": "{Many} ranked by {v}, lowest first",
}
ACROSS_EVENTS = {
    "mean": "Average {v} of {ev}",
    "median": "Median {v} of {ev}",
    "sum": "Sum of the {v} of {ev}",
    "max": "The largest of {ev}, by {v}",
    "min": "The smallest of {ev}, by {v}",
    "top": "{Ev} ranked by {v}, largest first",
    "bottom": "{Ev} ranked by {v}, smallest first",
    "latest": "The most recent of {ev}",
    "earliest": "The earliest of {ev}",
}


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------


def _round_value(entry: dict, value):
    if value is None:
        return None
    # counts and event durations are whole numbers of days/events
    whole = entry["aggregation"] == "count" or (
        entry.get("unit") == "event" and entry.get("event_value") == "duration"
    )
    return int(value) if whole else round(value, 1)


def _converted_rows(entry: dict, rows: list[dict], sentence: bool = False) -> list[dict]:
    """
    A top/bottom result's rows with output_unit applied to each value and
    rounded, the same way a single value is. An event row's total is PRCP or
    SNOW (never converted), so it's only rounded.
    """
    out = []
    for row in rows:
        row = dict(row)
        row["value"] = _round_value(entry, convert_value(entry, row.get("value")))
        if row.get("bucket_year") and (label := season_label(entry, row["bucket_year"], sentence)):
            del row["bucket_year"]
            row["season"] = label
        if row.get("total") is not None:
            row["total"] = round(row["total"], 1)
        out.append(row)
    return out


def _metric_name(metric) -> str:
    return METRIC_LABELS.get(metric, str(metric)).lower()


def _celsius(entry: dict) -> bool:
    return entry.get("output_unit") == "C"


def _months_text(months) -> str:
    """'Jun-Aug' for a run of months, else 'Jan, Mar, Jul'."""
    names = [calendar.month_abbr[m] for m in months]
    run = list(range(months[0], months[0] + len(months)))
    if len(months) > 2 and list(months) == run:
        return f"{names[0]}-{names[-1]}"
    return ", ".join(names)


def _when(entry: dict) -> str:
    start, end = entry["start_date"], entry["end_date"]
    if (start, end) == (f"{NORMAL_START_YEAR}-01-01", f"{NORMAL_END_YEAR}-12-31"):
        return f"the {NORMAL_START_YEAR}-{NORMAL_END_YEAR} normals period"
    if start == ALL_TIME_START:
        return f"the whole record through {_fmt_day(end)}"
    return _fmt_span(start, end)


def _bucket_names(entry: dict) -> tuple[str, str]:
    unit = entry["unit"]
    if unit == "season":
        return entry["season"], f"{entry['season']} seasons"
    return BUCKET_NAMES[unit]


def _bucket_note(entry: dict) -> str:
    """' (summer: Jun-Aug)', or the snow season's months."""
    if entry["unit"] == "snow_season":
        return SNOW_SEASON_NOTE
    if entry["unit"] == "season":
        first, last = SEASON_MONTHS[entry["season"]]
        return f" ({entry['season']}: {calendar.month_abbr[first]}-{calendar.month_abbr[last]})"
    return ""


def _bucket_value(entry: dict) -> str:
    """'total snowfall', 'average high temperature': how each bucket's days were combined."""
    per = entry.get("period_aggregation") or natural_aggregation(entry["metric"])
    return f"{'total' if per == 'sum' else 'average'} {_metric_name(entry['metric'])}"


def _describe_buckets(entry: dict) -> str:
    one, many = _bucket_names(entry)
    value = _bucket_value(entry)
    agg = entry["aggregation"]
    if agg == "count":
        label = threshold_label(entry["metric"], entry["threshold"], _celsius(entry))
        what = f"Number of {many} with {value.split()[0]} {label}"
    else:
        what = ACROSS_BUCKETS[agg].format(one=one, v=value, Many=many.capitalize())
    if entry.get("month_filter") and entry["unit"] == "month":
        what += f" ({_months_text(entry['month_filter'])} only)"
    return what + _bucket_note(entry)


def _describe_events(entry: dict) -> str:
    metric, agg = entry["metric"], entry["aggregation"]
    day = threshold_label(metric, entry["event_day_threshold"], _celsius(entry))
    ev = f"events (runs of consecutive days with {day})"
    duration = entry.get("event_value") == "duration"
    value = "length in days" if duration else f"total {_metric_name(metric)}"
    if agg == "count":
        what = f"Number of {ev}"
    else:
        what = ACROSS_EVENTS[agg].format(v=value, ev=f"the {ev}", Ev=ev.capitalize())
    if (t := entry.get("threshold")) and agg not in ("top", "bottom"):
        sign = threshold_label(metric, t).split()[1]
        if duration:
            what += f", counting only events lasting {sign} {t['value']} days"
        else:
            what += f", counting only events with a total {threshold_label(metric, t, _celsius(entry))}"
    return what


def _describe_calendar_day(entry: dict) -> str:
    day = calendar_day_label(entry["calendar_day"])
    metric = _metric_name(entry["metric"])
    if entry.get("period_aggregation") == "any":
        label = threshold_label(entry["metric"], entry["threshold"], _celsius(entry))
        return f"Percent of years with {label} on {day}"
    if entry["aggregation"] in ("max", "min"):
        word = "Highest" if entry["aggregation"] == "max" else "Lowest"
        return f"{word} {metric} on record for {day}"
    smoothing = f"smoothed over +/-{NORMAL_SMOOTHING_DAYS} days"
    return f"Average {metric} on {day} ({smoothing})"


def _describe_dates(entry: dict) -> str:
    which = "First" if entry["aggregation"] == "first_date" else "Last"
    unit = entry.get("unit", "year")
    label = threshold_label(entry["metric"], entry["threshold"], _celsius(entry))
    what = f"{which} date each {BUCKET_NAMES[unit][0]} with {label}"
    if entry.get("month_filter"):
        what += f", searched in {_months_text(entry['month_filter'])}"
    return what + (SNOW_SEASON_NOTE if unit == "snow_season" else "")


def _fmt_day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d:%b} {d.day}, {d.year}"


def _fmt_span(start: str, end: str) -> str:
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    if s == e:
        return _fmt_day(start)
    if (s.year, s.month) == (e.year, e.month):
        return f"{s:%b} {s.day}–{e.day}, {s.year}"
    if s.year == e.year:
        return f"{s:%b} {s.day} – {e:%b} {e.day}, {s.year}"
    return f"{_fmt_day(start)} – {_fmt_day(end)}"


def _period_name(entry: dict, bucket: dict) -> str:
    """'Jan 2000', '2000', 'winter 2009-10', '2009-10 snow season': one bucket, as the answer names it."""
    year, unit = bucket["bucket_year"], entry.get("unit")
    if unit == "snow_season":
        return f"{season_label(entry, year)} snow season"
    if unit == "season":
        return season_label(entry, year, sentence=True)
    if unit == "month" and bucket.get("bucket_month"):
        return f"{calendar.month_abbr[bucket['bucket_month']]} {year}"
    return str(year)


def _periods_counted(entry: dict, result: dict) -> dict:
    """How many periods a month/year/season answer covers, and the first and last of them."""
    if result.get("n_buckets") is None:
        return {}  # the SQL function predates these fields
    out = {"periods_counted": result["n_buckets"]}
    if result.get("first_bucket"):
        out["first_period"] = _period_name(entry, result["first_bucket"])
        out["last_period"] = _period_name(entry, result["last_bucket"])
    return out


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------


def describe(entry: dict) -> str:
    """
    One sentence saying what a query computed, from its fields after
    apply_defaults: the statistic, which days or periods, any threshold or
    month filter, and the dates. For an answer to rest on (a season isn't
    read as a year, a count's threshold is the one used).
    """
    unit = entry.get("unit", "day")
    agg = entry["aggregation"]
    if agg in ("first_date", "last_date"):
        what = _describe_dates(entry)
    elif unit == "day_of_year":
        what = _describe_calendar_day(entry)
    elif unit == "event":
        what = _describe_events(entry)
    elif unit in ("month", "year", "season", "snow_season"):
        what = _describe_buckets(entry)
    elif agg == "count":
        label = threshold_label(entry["metric"], entry["threshold"], _celsius(entry))
        what = f"Number of days with {label}"
    else:
        what = DAILY_STATS[agg].format(m=_metric_name(entry["metric"]))
    if entry.get("month_filter") and unit == "day":
        what += f" in {_months_text(entry['month_filter'])}"
    # TAVG and AWND are already averages: "average daily temperature", not "average daily average"
    what = what.replace("daily average ", "daily ").replace("verage average ", "verage ")
    return f"{what}, {_when(entry)}."


def excluded(entry: dict) -> str | None:
    """What the SQL functions' coverage rule leaves out of a month/year/season answer; None for daily answers."""
    unit = entry.get("unit", "day")
    if unit not in ("month", "year", "season", "snow_season") or entry["aggregation"] in ("first_date", "last_date"):
        return None
    many = _bucket_names(entry)[1]
    share = f"{MIN_BUCKET_COVERAGE:.0%}"
    return (
        f"{many.capitalize()} with under {share} of their days observed within these dates are left out, "
        f"so one cut off by the start or end date isn't counted."
    )


def build_answer_payload(
    entry: dict, result: dict | None, station_info: dict, temp_unit: str = "F"
) -> dict:
    """
    Packages one query's parameters + its answer_query result into the plain
    facts the answer-generation model needs. No phrasing decisions are made
    here. The model turns this (plus the original question) into the final
    sentence; this function just makes sure the value it's given is exactly
    the one that should appear, so it has nothing to compute.
    """
    # User's Celsius preference, unless the query already picked a unit.
    if (
        temp_unit == "C"
        and entry.get("output_unit") is None
        and entry["metric"] in TEMP_METRICS
    ):
        entry = {**entry, "output_unit": "C"}

    station = station_info.get(entry["station_id"], {})
    odds = entry.get("period_aggregation") == "any"
    dates = entry["aggregation"] in ("first_date", "last_date")
    value = (result or {}).get("value") if odds or dates else _round_value(
        entry, convert_value(entry, result.get("value") if result else None)
    )

    payload = {
        "station_name": station.get("name") or entry["station_id"],
        "place": station_place(station, entry["station_id"]),
        "metric": entry["metric"],
        "aggregation": entry["aggregation"],
        "units": "percent of years" if odds else "date" if dates else answer_units(entry),
        "start_date": entry["start_date"],
        "end_date": entry["end_date"],
        "value": value,
        "computed": describe(entry),
    }
    if note := excluded(entry):
        payload["excluded"] = note
    if result:
        for key in (
            "obs_date",
            "event_start",
            "event_end",
            "bucket_year",
            "bucket_month",
            "calendar_day",
            "record_year",
            "n_years",
            "typical_date",
            "earliest_on_record",
            "latest_on_record",
            "data_years",
            "years_counted",
            "years_without",
        ):
            if result.get(key) is not None:
                payload[key] = result[key]
        # a season isn't a calendar year: name it so it isn't read as one
        if "bucket_year" in payload and (label := season_label(entry, payload["bucket_year"], sentence=True)):
            del payload["bucket_year"]
            payload["season"] = label
        if "excluded" in payload:
            payload.update(_periods_counted(entry, result))
        if result.get("ties"):
            payload["ties"] = _converted_rows(entry, result["ties"], sentence=True)
        if odds or dates:
            payload["condition"] = entry.get("threshold")
        if result.get("matching_days"):
            # rounded/converted like a single reading of the metric, not a count
            payload["matching_days"] = _converted_rows({**entry, "aggregation": "max"}, result["matching_days"])
        # top/bottom: value is null and the ranking is in rows (rank, then
        # date). Ties share a rank, so rows can outnumber the requested ranks.
        if entry["aggregation"] in RANKING_AGGREGATIONS:
            payload["unit"] = entry.get("unit", "day")
            if entry.get("season"):
                payload["season_name"] = entry["season"]
            payload["rows"] = _converted_rows(entry, result.get("rows") or [])
            payload["rows_truncated"] = bool(result.get("rows_truncated"))
            if entry.get("requested_limit"):
                payload["requested_limit"] = entry["requested_limit"]
            if entry.get("unit") == "event":
                payload["event_value"] = entry.get("event_value", "total")
    return payload


def ranking_chart(payload: dict, table: dict) -> dict:
    """
    A /plot of a ranking: its rows as horizontal bars, labeled the way the
    table labels them (date, event dates, month, season, or year).
    """
    label_col = next(k for k in table["rows"][0] if k not in ("Rank", "Value", "Total", "Days")) if table["rows"] else "Date"
    points = [
        {"x": i, "label": row[label_col], "rank": r["rank"],
         "value": r.get("duration") if payload.get("event_value") == "duration" else r["value"]}
        for i, (r, row) in enumerate(zip(payload.get("rows", []), table["rows"]))
    ]
    units = "days" if payload.get("event_value") == "duration" else payload["units"]
    return {
        "kind": "ranking", "unit": payload.get("unit", "day"), "units": units,
        "title": table["title"], "heading": table["title"].split(" · ")[0], "subtitle": payload["place"],
        "series": [{"name": payload["metric"], "metric": payload["metric"], "bars": True, "trend": None, "points": points}],
        "notes": [table["note"]] if table.get("note") else [],
    }


def ranking_table(payload: dict) -> dict | None:
    """
    A display table for one top/bottom answer payload, or None for any other
    aggregation: {"title", "rows": [{column: text}], "note"}. Values carry
    their units so the table reads on its own; formatting only, no new data.
    """
    if payload.get("aggregation") not in RANKING_AGGREGATIONS:
        return None
    units = payload["units"]
    unit_suffix = units if units.startswith("°") else f" {units}" if units else ""
    by_duration = payload.get("event_value") == "duration"

    rows = []
    for r in payload.get("rows", []):
        if "event_start" in r:
            row = {
                "Rank": r["rank"],
                "Dates": _fmt_span(r["event_start"], r["event_end"]),
            }
        elif "obs_date" in r:
            row = {"Rank": r["rank"], "Date": _fmt_day(r["obs_date"])}
        elif r.get("bucket_month"):
            row = {
                "Rank": r["rank"],
                "Month": f"{date(r['bucket_year'], r['bucket_month'], 1):%b %Y}",
            }
        elif "season" in r:
            row = {"Rank": r["rank"], "Season": r["season"]}
        else:
            row = {"Rank": r["rank"], "Year": str(r["bucket_year"])}
        row["Value"] = f"{r['value']}{unit_suffix}"
        if "duration" in r:
            # show the event's other measure alongside the ranked one
            if by_duration:
                metric_units = METRIC_UNITS.get(payload["metric"], "")
                row["Total"] = f"{r['total']} {metric_units}".strip()
            else:
                row["Days"] = r["duration"]
        rows.append(row)

    which = "Top" if payload["aggregation"] == "top" else "Bottom"
    grain = (
        f"{payload['season_name']}s"  # "Top summers by ..."
        if payload.get("unit") == "season"
        else COUNT_UNITS.get(payload.get("unit", "day"), "days")
    )
    label = (
        "length"
        if by_duration
        else METRIC_LABELS.get(payload["metric"], payload["metric"]).lower()
    )
    notes = []
    if payload.get("requested_limit"):
        notes.append(
            f"You asked for {payload['requested_limit']}; rankings show at most {MAX_RANK_LIMIT}, "
            f"so this is the {payload['aggregation']} {MAX_RANK_LIMIT}."
        )
    if payload.get("rows_truncated"):
        notes.append("More rows are tied within these ranks than are shown.")
    note = " ".join(notes) or None
    return {
        # e.g. "Top events by snowfall · Sterling, VA"
        "title": f"{which} {grain} by {label} · {payload['place']}",
        "rows": rows,
        "note": note,
    }
