"""
Shared pipeline library for the weather-query-resolver skill.

Runs the real production code: a real Claude API call using SKILL.md as the
system prompt (with skills/references/count_group.md appended only when the
question needs it), a Databricks SQL connection for lookup_station and
answer_query, and resolve_date_range tool run locally in-process.

This matches the decided architecture: production is a separate app calling
an AI model directly via API rather than a Databricks-hosted AI agent, and
only the two tools whose data actually live in Unity Catalog:

- lookup_station
- answer_query

This module is the single source of truth for that pipeline and has no CLI
and no chat-history persistence of its own. Both app/test_harness.py (a CLI)
and app/chat_session.py (a non-CLI session wrapper for a future Streamlit
chat UI) import these same functions, and each does its own chat_store.py
persistence around them.

Requires (add to your .env, loaded via python-dotenv):
    ANTHROPIC_API_KEY   (used by callers to construct the anthropic.Anthropic
                          client passed into resolve_question/generate_answer;
                          this module never imports the anthropic package
                          itself)
    DATABRICKS_SERVER
    DATABRICKS_HTTP     (SQL warehouse connection details page in Databricks)
    DATABRICKS_CLIENT   (service principal application/client ID)
    DATABRICKS_SECRET   (service principal OAuth secret)

NOTE: the exact parameter-binding syntax below (":name" style, passed via
the `parameters=` dict to cursor.execute) is databricks-sql-connector's
native-parameters feature.
"""

import sys
import os
import re
import calendar
import json
from datetime import date
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from databricks import sql as databricks_sql
from databricks.sdk.core import Config, oauth_service_principal

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILL_PATH = PROJECT_ROOT / "skills" / "SKILL.md"
COUNT_GROUP_PATH = PROJECT_ROOT / "skills" / "references" / "count_group.md"
ANSWER_PHRASING_PATH = PROJECT_ROOT / "skills" / "references" / "answer_phrasing.md"
PLOTTING_PATH = PROJECT_ROOT / "skills" / "references" / "plotting.md"
SCRIPTS_PATH = PROJECT_ROOT / "skills" / "scripts"

CLAUDE_MODEL = "claude-haiku-4-5-20251001"

# Unity Catalog catalog holding the analytics functions/views this app calls.
# Defaults to prod; set WX_CATALOG=weather_dev in .env to run against dev.
CATALOG = os.environ.get("WX_CATALOG", "weather")

# Import the resolve_date_range code used by the skill directly
sys.path.insert(0, str(SCRIPTS_PATH))
from resolve_date_range import resolve as _resolve_date_range_core  # type: ignore # noqa: E402

# ---------------------------------------------------------------------------
# Databricks calls the two tools whose data actually lives in Unity
# Catalog. answer_query is called by this app code directly, AFTER the
# model call returns; it is not a tool the model itself calls.
# ---------------------------------------------------------------------------


def _service_principal_credentials():
    config = Config(
        host=f"https://{os.environ['DATABRICKS_SERVER']}",
        client_id=os.environ["DATABRICKS_CLIENT"],
        client_secret=os.environ["DATABRICKS_SECRET"],
    )
    return oauth_service_principal(config)


def get_databricks_connection():
    return databricks_sql.connect(
        server_hostname=os.environ["DATABRICKS_SERVER"],
        http_path=os.environ["DATABRICKS_HTTP"],
        credentials_provider=_service_principal_credentials,
    )


def call_lookup_station(conn, location_text: str):
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {CATALOG}.analytics.lookup_station(:location_text)",
            parameters={"location_text": location_text},
        )
        row = cur.fetchone()
    if not row or row[0] is None:
        return None
    return json.loads(row[0])


def fill_station_info(conn, resolved: dict, station_info: dict) -> None:
    """
    Adds stations the resolver used without calling lookup_station this
    session, when the id is a real station. The skill's worked examples use
    real ids (Dulles above all), so the model sometimes reuses one from memory;
    the id is right, but validate_resolved only trusts lookup results. An id
    that isn't in the stations table stays missing and is still rejected.
    """
    missing = {
        q.get("station_id")
        for q in resolved.get("queries") or []
        if isinstance(q, dict) and isinstance(q.get("station_id"), str)
    } - set(station_info)
    if not missing:
        return
    params = {f"id{i}": sid for i, sid in enumerate(sorted(missing))}
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT ghcn_id, name, city FROM {CATALOG}.analytics.stations "
            f"WHERE ghcn_id IN ({', '.join(':' + k for k in params)})",
            parameters=params,
        )
        rows = cur.fetchall()
    for ghcn_id, name, city in rows:
        station_info[ghcn_id] = {"ghcn_id": ghcn_id, "name": name, "city": city}


def list_home_locations(conn) -> dict[str, str]:
    """
    {station code: display name} for the home-location picker, ordered by
    name, e.g. {"KIAD": "Sterling, VA (KIAD)"}. The code (ICAO, else IATA) is
    the value saved as home_location and given to the resolver -- an exact
    lookup_station key. The display name is presentation only. A city that
    already carries its own qualifier (e.g. "Washington, DC", stored with
    state VA) is used as-is.
    """
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT coalesce(icao, iata) AS code, city, state
            FROM {CATALOG}.analytics.stations
            WHERE coalesce(icao, iata) IS NOT NULL AND city IS NOT NULL
            ORDER BY city, code
            """)
        rows = cur.fetchall()
    return {
        code: f"{city if ',' in city or not state else f'{city}, {state}'} ({code})"
        for code, city, state in rows
    }


SNOW_SEASON_START_MONTH = 7  # NWS snow season: July 1 - June 30

# Meteorological seasons. Winter's year starts in December so Dec-Feb stays
# one bucket, labeled by the year it ends in (Dec 2009 - Feb 2010 = 2010).
SEASON_MONTHS = {
    "winter": [12, 1, 2],
    "spring": [3, 4, 5],
    "summer": [6, 7, 8],
    "fall": [9, 10, 11],
}
SEASON_START_MONTH = {"winter": 12}


def _sql_bucketing(entry: dict) -> tuple[str, list | None, int | None]:
    """
    (p_unit, p_month_filter, p_year_start_month) for answer_query. The
    resolver's snow_season and season units are year buckets in SQL: a
    snow season starts in July; a season keeps only its months.
    """
    unit = entry.get("unit", "day")
    if unit == "snow_season":
        return "year", None, SNOW_SEASON_START_MONTH
    if unit == "season":
        season = entry["season"]
        return "year", SEASON_MONTHS[season], SEASON_START_MONTH.get(season)
    return unit, entry.get("month_filter"), None


NORMAL_SMOOTHING_DAYS = 7  # a daily normal averages +/- this many days


def smooth_day_of_year(values: list) -> list:
    """
    Daily normals smoothed over +/- NORMAL_SMOOTHING_DAYS, wrapping across the
    year end (Dec 28 uses early January too), so 30 years of noisy single days
    read as a curve. Nulls are skipped; the input's 366 days are in order.
    """
    n, w, out = len(values), NORMAL_SMOOTHING_DAYS, []
    for i in range(n):
        window = [values[(i + k) % n] for k in range(-w, w + 1) if values[(i + k) % n] is not None]
        out.append(sum(window) / len(window) if window else None)
    return out


def calendar_day_label(mm_dd: str) -> str:
    """'07-04' -> 'July 4'."""
    m, d = (int(x) for x in mm_dd.split("-"))
    return f"{calendar.month_name[m]} {d}"


def _calendar_day_result(entry: dict, result: dict | None) -> dict | None:
    """
    One calendar day out of a day_of_year result, as a single-value result:
    the record (max/min, with the year set) or the smoothed normal (mean).
    """
    if not result or result.get("error") or entry["calendar_day"] not in result.get("days", []):
        return {"value": None}
    i = result["days"].index(entry["calendar_day"])
    agg = entry["aggregation"]
    out = {"calendar_day": calendar_day_label(entry["calendar_day"]), "n_years": result["n_years"][i]}
    if entry.get("period_aggregation") == "any":
        # odds: the share of years the condition held on that day, as a percent
        out["value"] = round(result["values"][i] * 100, 1)
    elif agg in ("max", "min"):
        out.update(value=result[agg][i], record_year=result[f"{agg}_year"][i])
    else:
        out["value"] = smooth_day_of_year(result["values"])[i]
    return out


def season_day(iso: str, snow_season: bool = False) -> int:
    """
    The date's day in its bucket, for comparing years: days since Jan 1, or
    since Jul 1 for a Jul-Jun season (an October snow is earlier than a
    January one).
    """
    d = date.fromisoformat(iso)
    if snow_season:
        start = date(d.year if d.month >= 7 else d.year - 1, 7, 1)
    else:
        start = date(d.year, 1, 1)
    return (d - start).days


def _date_result(entry: dict, result: dict | None) -> dict:
    """
    first_date / last_date answers: one year -> that date; several -> the
    earliest and latest (by time of year) with their years, the median date,
    and how many years had no such day.
    """
    field = "first" if entry["aggregation"] == "first_date" else "last"
    if not result or result.get("error"):
        return {"value": None}
    pairs = [(y, d) for y, d in zip(result.get("years", []), result.get(field, []))]
    found = [(y, d) for y, d in pairs if d]
    if len(pairs) == 1:
        return {"value": found[0][1] if found else None, "bucket_year": pairs[0][0]}
    if not found:
        return {"value": None, "years_counted": len(pairs)}
    snow = entry.get("unit") == "snow_season"
    by_day = sorted(found, key=lambda yd: season_day(yd[1], snow))
    mid = by_day[len(by_day) // 2][1]
    # every year that shares the earliest (or latest) day: a tie is reported, not broken
    tied = lambda end: [d for _, d in found if season_day(d, snow) == season_day(end, snow)]
    one_or_all = lambda ds: ds[0] if len(ds) == 1 else sorted(ds)
    years = [y for y, _ in pairs]
    return {"value": None, "typical_date": date.fromisoformat(mid).strftime("%B %-d"),
            "earliest_on_record": one_or_all(tied(by_day[0][1])), "latest_on_record": one_or_all(tied(by_day[-1][1])),
            "data_years": f"{min(years)}-{max(years)}", "years_counted": len(pairs), "years_without": len(pairs) - len(found)}


def call_answer_query(conn, entry: dict):
    # A record or normal for one calendar day ("record high on July 4") is a
    # day_of_year series in SQL, reduced to that day here
    if entry.get("unit") == "day_of_year" and entry["aggregation"] != "series":
        return _calendar_day_result(entry, call_answer_query(conn, {**entry, "aggregation": "series"}))
    # First/last date ("last spring freeze in 2025"): a dates series, reduced here
    if entry["aggregation"] in ("first_date", "last_date"):
        return _date_result(entry, call_answer_query(
            conn, {**entry, "aggregation": "series", "period_aggregation": entry["aggregation"]}))
    sql_unit, month_filter, year_start_month = _sql_bucketing(entry)
    threshold = entry.get("threshold") or {}
    event_day_threshold = entry.get("event_day_threshold") or {}
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {CATALOG}.analytics.answer_query(
                p_station_id => :station_id,
                p_metric => :metric,
                p_aggregation => :aggregation,
                p_start_date => :start_date,
                p_end_date => :end_date,
                p_unit => :unit,
                p_threshold_operator => :threshold_operator,
                p_threshold_value => :threshold_value,
                p_event_day_threshold_operator => :event_day_threshold_operator,
                p_event_day_threshold_value => :event_day_threshold_value,
                p_event_value => :event_value,
                p_period_aggregation => :period_aggregation,
                p_month_filter => from_json(:month_filter, 'ARRAY<INT>'),
                p_limit => :limit,
                p_year_start_month => :year_start_month
            )
            """,
            parameters={
                "station_id": entry["station_id"],
                "metric": entry["metric"],
                "aggregation": entry["aggregation"],
                "start_date": entry["start_date"],
                "end_date": entry["end_date"],
                "unit": sql_unit,
                "threshold_operator": threshold.get("operator"),
                "threshold_value": threshold.get("value"),
                "event_day_threshold_operator": event_day_threshold.get("operator"),
                "event_day_threshold_value": event_day_threshold.get("value"),
                "event_value": entry.get("event_value"),
                "period_aggregation": entry.get("period_aggregation"),
                # JSON text: the connector does not bind a Python list as a SQL ARRAY
                "month_filter": json.dumps(month_filter) if month_filter else None,
                "limit": entry.get("limit"),
                "year_start_month": year_start_month,
            },
        )
        row = cur.fetchone()
    if not row or row[0] is None:
        return None
    return json.loads(row[0])


# ---------------------------------------------------------------------------
# Primary application tools
# ---------------------------------------------------------------------------


def resolve_date_range(payload: dict) -> dict:
    """
    Adapts the real resolve()'s (start, end) date tuple into the
    {"start_date", "end_date"} string shape this harness's tool-result
    protocol expects. All the actual date logic lives in
    skills/scripts/resolve_date_range.py.
    """
    start, end = _resolve_date_range_core(payload)
    return {"start_date": start.isoformat(), "end_date": end.isoformat()}


# Physical unit each metric is stored in (the pipeline converts to imperial).
_METRIC_UNITS = {
    "TAVG": "°F",
    "TMAX": "°F",
    "TMIN": "°F",
    "PRCP": "inches",
    "SNOW": "inches",
    "SNWD": "inches",
    "AWND": "mph",
    "HDD": "heating degree days",
    "CDD": "cooling degree days",
}
# When aggregation is `count`, the value counts analysis-grain things, not
# the metric -- so the unit is the plural grain word.
_COUNT_UNITS = {
    "day": "days",
    "event": "events",
    "month": "months",
    "year": "years",
    "snow_season": "snow seasons",
    "season": "seasons",
}

_TEMP_METRICS = {"TAVG", "TMAX", "TMIN"}

# How many prior turns of this session to replay to the resolver.
MAX_CONTEXT_TURNS = 6


def answer_units(entry: dict) -> str:
    """Physical unit the answer value should be stated in."""
    grain = entry.get("unit", "day")
    if entry["aggregation"] == "count":
        return _COUNT_UNITS.get(grain, "days")
    if grain == "event" and entry.get("event_value") == "duration":
        return "days"
    if entry.get("output_unit") == "C" and entry["metric"] in _TEMP_METRICS:
        return "°C"
    return _METRIC_UNITS.get(entry["metric"], "")


def convert_value(entry: dict, value):
    """
    Apply the query's output_unit to the raw value. Only Fahrenheit ->
    Celsius, and only for temperature metrics. A stddev is a spread, so it
    scales by 5/9 without the 32-degree offset; everything else passes
    through unchanged.
    """
    if value is None:
        return None
    if entry.get("output_unit") == "C" and entry["metric"] in _TEMP_METRICS:
        if entry["aggregation"] == "stddev":
            return value * 5 / 9
        return (value - 32) * 5 / 9
    return value


RANKING_AGGREGATIONS = {"top", "bottom"}


def _round_value(entry: dict, value):
    if value is None:
        return None
    # counts and event durations are whole numbers of days/events
    whole = entry["aggregation"] == "count" or (
        entry.get("unit") == "event" and entry.get("event_value") == "duration"
    )
    return int(value) if whole else round(value, 1)


def snow_season_label(bucket_year: int) -> str:
    """A snow season bucket is labeled by the year it ends in: 2010 -> "2009-10"."""
    return f"{bucket_year - 1}-{bucket_year % 100:02d}"


def season_label(entry: dict, bucket_year: int) -> str | None:
    """
    How a snow_season/season bucket reads ("2009-10", "Winter 2009-10",
    "Summer 2024"), so it's never mistaken for a calendar year; None for any
    other unit.
    """
    unit = entry.get("unit")
    if unit == "snow_season":
        return snow_season_label(bucket_year)
    if unit == "season":
        season = entry["season"]
        span = snow_season_label(bucket_year) if season == "winter" else str(bucket_year)
        return f"{season.capitalize()} {span}"
    return None


MATCHING_DAYS_MAX = 5  # a day count this small also names the days


def matching_days(conn, entry: dict, result: dict | None) -> list[dict] | None:
    """
    The days behind a small daily count ("has DCA ever hit 80 in January?"
    -> 1, on Jan 26, 2024), so the answer can name them. A count only
    returns the number; this ranks the days the threshold points at (top for
    >/>=, bottom for </<=) and keeps those that meet it. None when the count
    is 0, too large, or not a day count.
    """
    t = entry.get("threshold") or {}
    n = (result or {}).get("value")
    if (entry["aggregation"] != "count" or entry.get("unit", "day") != "day" or t.get("operator") not in (">", ">=", "<", "<=")
            or not isinstance(n, (int, float)) or not 1 <= n <= MATCHING_DAYS_MAX):
        return None
    ranked = {k: v for k, v in entry.items() if k != "threshold"} | {
        "aggregation": "top" if t["operator"] in (">", ">=") else "bottom", "limit": int(n)}
    rows = (call_answer_query(conn, ranked) or {}).get("rows") or []
    meets = {">": lambda v: v > t["value"], ">=": lambda v: v >= t["value"],
             "<": lambda v: v < t["value"], "<=": lambda v: v <= t["value"]}[t["operator"]]
    return [{"obs_date": r["obs_date"], "value": r["value"]} for r in rows if r.get("value") is not None and meets(r["value"])]


def _converted_rows(entry: dict, rows: list[dict]) -> list[dict]:
    """
    A top/bottom result's rows with output_unit applied to each value and
    rounded, the same way a single value is. An event row's total is PRCP or
    SNOW (never converted), so it's only rounded.
    """
    out = []
    for row in rows:
        row = dict(row)
        row["value"] = _round_value(entry, convert_value(entry, row.get("value")))
        if row.get("bucket_year") and (label := season_label(entry, row["bucket_year"])):
            del row["bucket_year"]
            row["season"] = label
        if row.get("total") is not None:
            row["total"] = round(row["total"], 1)
        out.append(row)
    return out


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
        and entry["metric"] in _TEMP_METRICS
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
        "place": station.get("city") or station.get("name") or entry["station_id"],
        "metric": entry["metric"],
        "aggregation": entry["aggregation"],
        "units": "percent of years" if odds else "date" if dates else answer_units(entry),
        "start_date": entry["start_date"],
        "end_date": entry["end_date"],
        "value": value,
    }
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
        if "bucket_year" in payload and (label := season_label(entry, payload["bucket_year"])):
            del payload["bucket_year"]
            payload["season"] = label
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


# Display names for the ranking table header.
_METRIC_LABELS = {
    "TAVG": "Average temperature",
    "TMAX": "High temperature",
    "TMIN": "Low temperature",
    "PRCP": "Precipitation",
    "SNOW": "Snowfall",
    "SNWD": "Snow depth",
    "AWND": "Average wind speed",
    "HDD": "Heating degree days",
    "CDD": "Cooling degree days",
}


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
                metric_units = _METRIC_UNITS.get(payload["metric"], "")
                row["Total"] = f"{r['total']} {metric_units}".strip()
            else:
                row["Days"] = r["duration"]
        rows.append(row)

    which = "Top" if payload["aggregation"] == "top" else "Bottom"
    grain = (
        f"{payload['season_name']}s"  # "Top summers by ..."
        if payload.get("unit") == "season"
        else _COUNT_UNITS.get(payload.get("unit", "day"), "days")
    )
    label = (
        "length"
        if by_duration
        else _METRIC_LABELS.get(payload["metric"], payload["metric"]).lower()
    )
    notes = []
    if payload.get("requested_limit"):
        notes.append(
            f"You asked for {payload['requested_limit']}; rankings show at most 15, "
            f"so this is the {payload['aggregation']} 15."
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


def _for_phrasing(payload: dict) -> dict:
    """
    What the phrasing model sees of one payload. A ranking's lead-in names
    only #1 (the table under it shows the rest), so it gets just the rank-1
    rows plus how many rows the table holds -- it can't list rows it never
    sees, however the prompt is followed.
    """
    rows = payload.get("rows")
    if rows is None:
        return payload
    out = {k: v for k, v in payload.items() if k != "rows"}
    out["rank_1"] = [r for r in rows if r.get("rank") == 1]
    out["table_rows"] = len(rows)
    return out


def generate_answer(client, question: str, results: list[dict], history=None) -> str:
    # answer_phrasing.md is loaded fresh; not appended to the resolver's
    # system prompt like count_group.md)
    parts = []
    if history:
        transcript = "\n".join(
            f'- User asked: "{h["question"]}"\n  You answered: {h["answer"]}'
            for h in history[-MAX_CONTEXT_TURNS:]
        )
        parts.append(f"Earlier in this conversation:\n{transcript}\n")
    parts.append(f"Original question: {question}\n")
    phrasing_results = [_for_phrasing(r) for r in results]
    parts.append(f"Results:\n{json.dumps(phrasing_results, indent=2)}")
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=512,
        system=ANSWER_PHRASING_PATH.read_text(),
        messages=[{"role": "user", "content": "\n".join(parts)}],
    )
    return "".join(
        block.text for block in response.content if block.type == "text"
    ).strip()


def resolve_question(
    client,
    conn,
    question: str,
    testing: bool = False,
    history: list | None = None,
    station_info: dict | None = None,
    home_location: str | None = None,
    plot_mode: str | None = None,
) -> dict:
    """
    Runs the Claude tool-use loop until the model returns its final JSON,
    and returns that JSON.

    station_info maps ghcn_id -> the lookup_station result for it. It is
    session-scoped: the caller passes the same dict every turn, and each
    lookup_station call this turn adds to it, so a later follow-up that
    inherits a station (no fresh lookup) can still name the place.

    history is this session's prior turns ({question, answer, query});
    it's rendered into the system prompt as a transcript so a follow-up can
    inherit parameters or refer back to an earlier result.
    """
    if station_info is None:
        station_info = {}
    system_prompt = build_system_prompt(question, history, station_info, home_location, plot_mode)
    messages: list[dict[str, Any]] = [{"role": "user", "content": question}]

    while True:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2048,
            system=system_prompt,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            text = "".join(
                block.text for block in response.content if block.type == "text"
            )
            if testing:
                print(f"[debug] stop_reason={response.stop_reason!r}")
                print(f"[debug] raw final response text:\n{text!r}\n")

            try:
                return json.loads(_extract_json(text))
            except json.JSONDecodeError:
                # SKILL.md's Step 6 defines a needs_clarification JSON shape
                # for exactly this case, but the model sometimes answers in
                # plain prose instead of following it (seen for vague
                # questions like "how's the weather been?"). Treat unparseable
                # final text as that prose *being* the clarification message,
                # rather than crashing on a question the skill already knows
                # how to represent.
                if testing:
                    print(
                        "[debug] final text wasn't JSON; treating it as a "
                        "clarification message"
                    )
                # from_prose: guardrails.validate_resolved only shows this
                # text if it's short and reads as a question.
                return {
                    "status": "needs_clarification",
                    "clarification_question": text.strip(),
                    "from_prose": True,
                }

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            result = execute_tool(conn, block.name, block.input)
            if block.name == "lookup_station" and result:
                station_info[result["ghcn_id"]] = result
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                }
            )
        messages.append({"role": "user", "content": tool_results})


# ---------------------------------------------------------------------------
# Secondary in-app tools
# ---------------------------------------------------------------------------


def _extract_json(text: str) -> str:
    """
    Models sometimes add prose or markdown fencing around the JSON despite
    Step 6 saying not to (seen from claude-haiku-4-5: a "**Step 6:**" header
    followed by a fenced ```json block). Pull the JSON out defensively
    rather than requiring byte-perfect output. Prefers a fenced block
    wherever it appears in the text; falls back to the outermost {...}.
    """
    fence_match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if fence_match:
        return fence_match.group(1)
    if "{" in text and "}" in text:
        return text[text.index("{") : text.rindex("}") + 1]
    return text


# ---------------------------------------------------------------------------
# Tool definitions for the Claude API. Only lookup_station & resolve_date_range.
# The model needs both to complete the resolved JSON itself (Step 2 / Step 5).
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "lookup_station",
        "description": (
            "Resolves free text (city, airport name/alias, ICAO or IATA code) to an "
            "active station. Returns {ghcn_id, name, city}, or nothing if there's no match."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"location_text": {"type": "string"}},
            "required": ["location_text"],
        },
    },
    {
        "name": "resolve_date_range",
        "description": (
            "Resolves a classified time expression to {start_date, end_date} in "
            "YYYY-MM-DD, per weather-query-resolver Step 5. kind is one of: "
            "absolute_year, absolute_year_range, absolute_month, absolute_day, "
            "season, relative_days, weekday, recent_month, all_time. Only include the fields "
            "that apply to the chosen kind."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "current_date": {"type": "string"},
                "kind": {
                    "type": "string",
                    "enum": [
                        "absolute_year",
                        "absolute_year_range",
                        "absolute_month",
                        "absolute_day",
                        "season",
                        "relative_days",
                        "weekday",
                        "recent_month",
                        "all_time",
                    ],
                },
                "year": {"type": "integer"},
                "month": {"type": "integer"},
                "day": {"type": "integer"},
                "start_year": {"type": "integer"},
                "end_year": {"type": "integer"},
                "season": {
                    "type": "string",
                    "enum": ["winter", "spring", "summer", "fall", "autumn"],
                },
                "days": {"type": "integer"},
                "weekday": {
                    "type": "string",
                    "enum": [
                        "monday",
                        "tuesday",
                        "wednesday",
                        "thursday",
                        "friday",
                        "saturday",
                        "sunday",
                    ],
                },
            },
            "required": ["current_date", "kind"],
        },
    },
]


def execute_tool(conn, name: str, tool_input: dict):
    if name == "lookup_station":
        return call_lookup_station(conn, tool_input["location_text"])
    if name == "resolve_date_range":
        return resolve_date_range(tool_input)
    raise ValueError(f"unknown tool: {name}")


# ---------------------------------------------------------------------------
# Whether the question needs count_group.md appended, applying Step 4's
# trigger rule in app code instead of as a model-invoked tool call;
# this is decided upfront, and applied to the system prompt.
# ---------------------------------------------------------------------------

# \w*storms? also catches compounds (snowstorm, rainstorm, thunderstorm),
# which a plain \bstorm\b misses.
# Month names load it too: "average January high" is a unit: "month" +
# month_filter climate average, defined in count_group.md. Plurals too, for
# rankings ("the coldest Januaries").
TRIGGER_PATTERN = re.compile(
    r"\b(events?|\w*storms?|blizzards?|spells?|waves?|in a row|months?|years?|seasons?|winters?|springs?|summers?|falls?|autumns?"
    r"|januar(?:y|ies)|februar(?:y|ies)|march(?:es)?|aprils?|mays?|junes?|julys?"
    r"|augusts?|septembers?|octobers?|novembers?|decembers?)\b",
    re.IGNORECASE,
)


def needs_count_group(question: str, history: list | None = None) -> bool:
    """
    True if this question -- or the recent conversation it may be following
    up on -- is about events/months/years. A follow-up like "narrow that to
    this winter" names no trigger word itself, so earlier turns count too:
    their questions (including ones that only got a clarification back) and
    any non-day query they resolved to.
    """
    if TRIGGER_PATTERN.search(question):
        return True
    for turn in (history or [])[-MAX_CONTEXT_TURNS:]:
        if TRIGGER_PATTERN.search(turn.get("question", "")):
            return True
        if (turn.get("query") or {}).get("unit", "day") != "day":
            return True
    return False


def build_system_prompt(
    question: str,
    history: list | None = None,
    station_info: dict | None = None,
    home_location: str | None = None,
    plot_mode: str | None = None,
) -> list[dict]:
    """
    The resolver's system prompt as two blocks: the skill text (the same for
    every question of a kind, so it's cached -- repeat reads cost a tenth),
    then this turn's details (date, plot mode, home city, conversation),
    which must come after the cache point or every call would miss.
    """
    skill_text = SKILL_PATH.read_text()
    skill_text = re.sub(r"^---\n.*?\n---\n\n", "", skill_text, count=1, flags=re.DOTALL)
    if plot_mode:
        # Up front, not only after the long skill text: without it the model
        # sometimes applies SKILL.md's single-value rules to a chart request
        skill_text = (
            "# PLOT MODE\n\n"
            + (
                "This turn is a `/plot` request. Follow `references/plotting.md` (appended below): every "
                "resolved query must use `aggregation: \"series\"`. If nothing can be charted, return "
                "`needs_clarification` asking what to chart. Never return `reason: \"needs_plot\"`.\n\n"
                if plot_mode == "plot"
                else "This turn follows a chart. Follow `references/plotting.md` (appended below): chart it "
                "with `series` if it asks for another chart, otherwise answer it as a regular question. "
                "Never return `reason: \"needs_plot\"`.\n\n"
            )
            + "---\n\n"
            + skill_text
        )

    # plotting.md builds on count_group.md's season/snow_season units, so a
    # plot turn loads both
    if plot_mode or needs_count_group(question, history):
        skill_text += "\n\n---\n\n" + COUNT_GROUP_PATH.read_text()
    if plot_mode:
        skill_text += "\n\n---\n\n" + PLOTTING_PATH.read_text()

    # Per-turn details: after the cache point
    turn_text = f"## Today's date\n\n{date.today().isoformat()}"
    if plot_mode:
        turn_text += "\n\n---\n\n## Plot mode\n\n" + (
            "This is a `/plot` turn: the user asked for a chart (plotting.md Step 1)."
            if plot_mode == "plot"
            else "This is a follow-up to a chart (no `/plot`): chart it if it asks for another "
            "chart, otherwise answer it as a regular question (plotting.md Step 1)."
        )

    # Fallback location when the question names none (SKILL.md Step 2).
    if home_location:
        turn_text += f'\n\n---\n\n## User home city\n\n"{home_location}"'

    # Replay recent turns as a transcript. SKILL.md's "Follow-up questions"
    # section tells the resolver how to use it: inherit from the most recent
    # Resolved query for an elliptical follow-up, or reconstruct an earlier
    # turn's query for a back-reference / unit conversion.
    recent = (history or [])[-MAX_CONTEXT_TURNS:]
    if recent:
        si = station_info or {}
        blocks = []
        for i, turn in enumerate(recent, 1):
            lines = [f"--- turn {i} ---", f'User asked: "{turn["question"]}"']
            query = turn.get("query")
            if query:
                lines.append("Resolved query: " + json.dumps(query))
                info = si.get(query.get("station_id")) or {}
                names = list(
                    dict.fromkeys(n for n in (info.get("name"), info.get("city")) if n)
                )
                if query.get("station_id") and names:
                    lines.append(
                        f"(station_id {query['station_id']} is "
                        + " / ".join(names)
                        + " -- one site, whatever name the user uses)"
                    )
            if turn.get("answer"):
                lines.append(f"Answer given: {turn['answer']}")
            blocks.append("\n".join(lines))
        turn_text += (
            "\n\n---\n\n## Conversation context\n\n"
            "Earlier turns in this session, oldest first; the last is the "
            'most recent resolved query. Apply the "Follow-up questions" '
            "rules above.\n\n" + "\n\n".join(blocks)
        )

    return [
        {"type": "text", "text": skill_text, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": turn_text},
    ]
