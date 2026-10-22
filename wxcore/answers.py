"""
Answer shaping: one query's parameters + its answer_query result as the
plain facts an answer is phrased from, and top/bottom results as a display
table or a ranking chart.
"""

from datetime import date

from wxcore.dates import season_label
from wxcore.db import station_place
from wxcore.units import COUNT_UNITS, METRIC_LABELS, METRIC_UNITS, TEMP_METRICS, answer_units, convert_value
from wxcore.validation import MAX_RANK_LIMIT, RANKING_AGGREGATIONS


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
        if "bucket_year" in payload and (label := season_label(entry, payload["bucket_year"], sentence=True)):
            del payload["bucket_year"]
            payload["season"] = label
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
