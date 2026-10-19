"""
Series (/plot) results: turns answer_query's series JSON into chart-ready data.

A series query (references/plotting.md) returns every month, year, or season
as a point. This module does everything between that result and the chart:

- baseline_entry: the extra query a "plot against normal" needs (the normals
  over the baseline years, at the target's grain)
- build_series: one chart line -- labels, unit conversion, normals matched
  by month / season / year, anomalies, the trend fit, notes
- group_charts: lines that share a chart kind and unit go on one chart
- phrasing_summary: the few numbers the answer sentence may use (never the
  raw points)

Chart drawing itself lives in charts.py; nothing here imports Altair.
"""

from __future__ import annotations

import calendar
from datetime import date

import numpy as np

import pipeline

CLIMATOLOGY_UNITS = {"month_of_year", "season_of_year"}
SEASON_NAMES = ["winter", "spring", "summer", "fall"]
# A normal needs at least this share of the baseline's years (24 of 30)
NORMAL_COMPLETENESS = 0.8
_TEMPS = {"TAVG", "TMAX", "TMIN"}
_TOTALS = {"PRCP", "SNOW"}


def baseline_entry(entry: dict) -> dict | None:
    """
    The query for a target series' normals: the baseline years at the
    target's grain (monthly -> month_of_year, a season -> season_of_year,
    yearly / snow season -> the same unit, averaged in build_series).
    None when the entry has no baseline.
    """
    baseline = entry.get("baseline")
    if not baseline:
        return None
    sy, ey = baseline["start_year"], baseline["end_year"]
    base = {
        "station_id": entry["station_id"],
        "metric": entry["metric"],
        "aggregation": "series",
        "period_aggregation": entry["period_aggregation"],
        **({"threshold": entry["threshold"]} if "threshold" in entry else {}),
        "start_date": f"{sy}-01-01",
        "end_date": f"{ey}-12-31",
    }
    unit = entry["unit"]
    if unit == "month":
        return {**base, "unit": "month_of_year"}
    if unit == "season":
        return {**base, "unit": "season_of_year"}
    if unit == "snow_season":
        # seasons ending in sy..ey (labeled by the year they end in)
        return {**base, "unit": "snow_season", "start_date": f"{sy - 1}-07-01", "end_date": f"{ey}-06-30"}
    return {**base, "unit": unit}


def _convert(entry: dict, temp_unit: str):
    """(value converter, spread converter) for the entry's output unit."""
    to_c = (
        not _is_count(entry)
        and entry["metric"] in _TEMPS
        and (entry.get("output_unit") or temp_unit) == "C"
    )
    if to_c:
        # a spread (stddev, anomaly) is a difference: no 32-degree offset
        return (lambda v: (v - 32) * 5 / 9), (lambda v: v * 5 / 9)
    return (lambda v: v), (lambda v: v)


def _is_count(entry: dict) -> bool:
    """A series of the number of days per bucket meeting a threshold."""
    return entry.get("period_aggregation") == "count"


_OPERATOR_SIGNS = {">": ">", ">=": "≥", "<": "<", "<=": "≤", "=": "="}


def _count_label(entry: dict, temp_unit: str) -> str:
    """'Days with high ≥ 90°F' -- the threshold in the user's units."""
    t = entry["threshold"]
    value = t["value"]
    unit = pipeline._METRIC_UNITS.get(entry["metric"], "")
    if entry["metric"] in _TEMPS:
        if (entry.get("output_unit") or temp_unit) == "C":
            value, unit = round((value - 32) * 5 / 9, 1), "°C"
        value = int(value) if float(value).is_integer() else value
        label = f"{value}{unit}"
    else:
        label = f"{value} {'in.' if unit == 'inches' else unit}".strip()
    what = pipeline._METRIC_LABELS.get(entry["metric"], entry["metric"]).lower()
    what = {"high temperature": "high", "low temperature": "low", "average temperature": "average temperature"}.get(what, what)
    return f"Days with {what} {_OPERATOR_SIGNS.get(t['operator'], t['operator'])} {label}"


def _units(entry: dict, temp_unit: str) -> str:
    if _is_count(entry):
        return "days"
    if entry["metric"] in _TEMPS:
        return "°C" if (entry.get("output_unit") or temp_unit) == "C" else "°F"
    return pipeline._METRIC_UNITS.get(entry["metric"], "")


_TWO_DECIMALS = {"PRCP", "SNOW", "SNWD"}


def _rounder(metric: str, count: bool = False):
    """Precipitation/snow to 2 decimals, everything else (and day counts) to 1 (as query_series)."""
    digits = 2 if metric in _TWO_DECIMALS and not count else 1
    return lambda v: None if v is None else round(float(v), digits)


def _time_points(entry: dict, result: dict) -> list[dict]:
    """day / month / year / season / snow_season results as {x, label, year, month, value}."""
    if entry["unit"] == "day":
        points = []
        for iso, v in zip(result.get("dates", []), result.get("values", [])):
            d = date.fromisoformat(iso)
            points.append({
                "x": iso, "x_num": d.year + (d.timetuple().tm_yday - 0.5) / 365.25,
                "label": f"{calendar.month_abbr[d.month]} {d.day}, {d.year}",
                "year": d.year, "month": d.month, "value": v,
            })
        return points
    points = []
    months = result.get("months") or [None] * len(result.get("years", []))
    for y, m, v in zip(result.get("years", []), months, result.get("values", [])):
        if entry["unit"] == "month":
            label = f"{calendar.month_abbr[m]} {y}"
            x = date(y, m, 1)
            x_num = y + (m - 0.5) / 12
        else:
            label = pipeline.season_label(entry, y) or str(y)
            x = date(y, 1, 1)
            x_num = float(y)
        points.append({"x": x.isoformat(), "x_num": x_num, "label": label, "year": y, "month": m, "value": v})
    return points


def _climatology_points(entry: dict, result: dict) -> list[dict]:
    if entry["unit"] == "month_of_year":
        keys = result.get("months", [])
        labels = [calendar.month_abbr[m] for m in keys]
    else:
        keys = [SEASON_NAMES.index(s) + 1 for s in result.get("seasons", [])]
        labels = [s.capitalize() for s in result.get("seasons", [])]
    return [
        {"x": k, "label": lab, "value": v, "stddev": sd, "n_years": n}
        for k, lab, v, sd, n in zip(
            keys, labels, result.get("values", []), result.get("stddev", []), result.get("n_years", [])
        )
    ]


def _normals(entry: dict, base_result: dict | None, notes: list[str]) -> dict | None:
    """
    {key: (normal, stddev)} for the target's grain -- month number, the
    season's single key, or None (one annual normal) -- keeping only normals
    with enough years (NORMAL_COMPLETENESS of the baseline).
    """
    if not base_result or base_result.get("error"):
        return None
    b = entry["baseline"]
    years = b["end_year"] - b["start_year"] + 1
    need = NORMAL_COMPLETENESS * years
    unit = entry["unit"]
    out, dropped = {}, 0
    if unit == "month":
        for m, v, sd, n in zip(base_result.get("months", []), base_result.get("values", []),
                               base_result.get("stddev", []), base_result.get("n_years", [])):
            if n >= need:
                out[m] = (v, sd)
            else:
                dropped += 1
    elif unit == "season":
        seasons = base_result.get("seasons", [])
        if entry["season"] in seasons:
            i = seasons.index(entry["season"])
            if base_result["n_years"][i] >= need:
                out[None] = (base_result["values"][i], base_result["stddev"][i])
            else:
                dropped += 1
    else:  # year / snow_season: average the baseline years here
        vals = base_result.get("values", [])
        if len(vals) >= need:
            out[None] = (float(np.mean(vals)), float(np.std(vals, ddof=1)) if len(vals) > 1 else 0.0)
        else:
            dropped += 1
    if dropped:
        notes.append(
            f"Normals need at least {int(NORMAL_COMPLETENESS * 100)}% of the "
            f"{b['start_year']}-{b['end_year']} years observed; "
            + ("none qualified." if not out else f"{dropped} normal value(s) left out.")
        )
    return out or None


def _trend(points: list[dict], key: str) -> dict | None:
    xs = [p["x_num"] for p in points if p.get(key) is not None]
    ys = [p[key] for p in points if p.get(key) is not None]
    if len(xs) < 3:
        return None
    slope, intercept = np.polyfit(xs, ys, 1)
    first, last = points[0], points[-1]
    return {
        "per_decade": round(float(slope) * 10, 2),
        "start": {"x": first["x"], "value": round(float(slope * first["x_num"] + intercept), 2)},
        "end": {"x": last["x"], "value": round(float(slope * last["x_num"] + intercept), 2)},
    }


def build_series(
    entry: dict,
    result: dict | None,
    base_result: dict | None,
    station_info: dict,
    temp_unit: str = "F",
) -> dict:
    """One chart line, JSON-safe so it can live in the session history."""
    station = station_info.get(entry["station_id"], {})
    unit = entry["unit"]
    climatology = unit in CLIMATOLOGY_UNITS
    kind = (
        "climatology" if climatology
        else entry.get("display", "overlay") if entry.get("baseline")
        else "time_series"
    )
    conv, spread = _convert(entry, temp_unit)
    _round = _rounder(entry["metric"], _is_count(entry))
    notes = list(entry.get("notes") or [])
    series = {
        "kind": kind,
        "unit": unit,
        "metric": entry["metric"],
        "metric_label": (
            _count_label(entry, temp_unit) if _is_count(entry)
            else pipeline._METRIC_LABELS.get(entry["metric"], entry["metric"])
        ),
        "count": _is_count(entry),
        "station_id": entry["station_id"],
        "place": station.get("city") or station.get("name") or entry["station_id"],
        "station_name": station.get("name") or entry["station_id"],
        "units": _units(entry, temp_unit),
        # totals and day counts are amounts per bucket: bars, from zero
        "bars": _is_count(entry) or (
            entry["metric"] in _TOTALS and (entry.get("period_aggregation") == "sum" or unit == "day")
        ),
        "baseline": (
            f"{entry['baseline']['start_year']}-{entry['baseline']['end_year']}" if entry.get("baseline") else None
        ),
        "season": entry.get("season"),
        "points": [],
        "trend": None,
        "notes": notes,
    }
    if not result or result.get("error"):
        return series

    if climatology:
        points = _climatology_points(entry, result)
        for p in points:
            p["value"] = _round(conv(p["value"]))
            p["stddev"] = _round(spread(p["stddev"])) if p["stddev"] is not None else None
            if p["stddev"] is not None:
                p["lower"], p["upper"] = _round(p["value"] - p["stddev"]), _round(p["value"] + p["stddev"])
        series["points"] = points
        if not entry.get("start_date", "").startswith("1800"):
            series["baseline"] = f"{entry['start_date'][:4]}-{entry['end_date'][:4]}"
        return series

    points = _time_points(entry, result)
    for p in points:
        p["value"] = _round(conv(p["value"]))

    normals = _normals(entry, base_result, notes) if entry.get("baseline") else None
    if entry.get("baseline"):
        for p in points:
            key = p["month"] if unit == "month" else None
            if normals and key in normals:
                normal, sd = normals[key]
                p["normal"] = _round(conv(normal))
                sd = spread(sd) if sd is not None else None
                p["lower"] = _round(p["normal"] - sd) if sd is not None else None
                p["upper"] = _round(p["normal"] + sd) if sd is not None else None
                p["anomaly"] = _round(p["value"] - p["normal"])

    if entry.get("trend"):
        series["trend"] = _trend(points, "anomaly" if kind == "anomaly" else "value")
    series["points"] = points
    return series


def group_charts(series_list: list[dict]) -> list[dict]:
    """
    Series that share a chart kind, grain, and unit of measure go on one
    chart (e.g. high and low together); anything else gets its own chart.
    """
    groups: dict[tuple, list[dict]] = {}
    for s in series_list:
        if s["points"]:
            groups.setdefault((s["kind"], s["unit"], s["units"], s["season"]), []).append(s)

    charts = []
    for (kind, unit, units, _), members in groups.items():
        places = list(dict.fromkeys(m["place"] for m in members))
        metrics = list(dict.fromkeys(m["metric_label"] for m in members))
        for m in members:
            # name each line by what differs between the lines
            parts = ([m["metric_label"]] if len(metrics) > 1 else []) + ([m["place"]] if len(places) > 1 else [])
            m["name"] = " · ".join(parts) or m["metric_label"]
        what = " & ".join(metrics)
        grain = {
            "day": "daily", "month": "monthly", "year": "yearly", "season": f"{members[0]['season'] or ''}s",
            "snow_season": "by snow season" if members[0]["metric"] == "SNOW" else "by cold season (Jul-Jun)",
            "month_of_year": "by month", "season_of_year": "by season",
        }[unit]
        period = members[0]["baseline"]
        if kind == "climatology":
            heading = f"{what} — {'monthly' if unit == 'month_of_year' else 'seasonal'} climatology"
            heading += f" ({period})" if period else ""
        elif kind == "overlay":
            heading = f"{what}, {grain} vs. {period} normal"
        elif kind == "anomaly":
            heading = f"{what}, {grain} departure from {period} normal"
        else:
            heading = f"{what}, {grain}"
        notes = list(dict.fromkeys(n for m in members for n in m["notes"]))
        if kind in ("overlay", "anomaly") or (kind == "climatology" and members[0]["baseline"]):
            notes.append(
                "Normals are computed from this station's observations and may differ slightly from NOAA's official normals."
            )
        # Subtitle: where (one place) and when (first to last point of a time series)
        sub = [places[0]] if len(places) == 1 else []
        if kind != "climatology":
            pts = sorted((p for m in members for p in m["points"]), key=lambda p: p["x"])
            # the heading already names a season ("summers"), so just the years
            label = (lambda p: str(p["year"])) if unit == "season" else (lambda p: p["label"])
            if pts:
                first, last = label(pts[0]), label(pts[-1])
                sub.append(first if first == last else f"{first} – {last}")
        chart = {
            "kind": kind,
            "unit": unit,
            "units": units,
            "title": f"{heading} · {places[0]}" if len(places) == 1 else heading,
            "heading": heading,
            "subtitle": " · ".join(sub),
            "series": members,
            "notes": notes,
        }
        if kind == "climatology":
            chart["table"] = _climatology_table(members, units)
        charts.append(chart)
    return charts


def _climatology_table(members: list[dict], units: str) -> list[dict]:
    rows = {}
    for m in members:
        for p in m["points"]:
            row = rows.setdefault(p["x"], {"": p["label"]})
            row[m["name"]] = f"{p['value']}{units if units.startswith('°') else ' ' + units}"
    return [rows[k] for k in sorted(rows)]


def phrasing_summary(series: dict) -> dict:
    """The numbers the answer sentence may cite -- never the raw points."""
    pts = [p for p in series["points"] if p.get("value") is not None]
    _r = _rounder(series["metric"], series.get("count", False))
    out = {
        "aggregation": "series",
        "chart": series["kind"],
        # what each point is, so the sentence doesn't call monthly means "daily"
        "grain": ("number of days meeting the threshold, " if series.get("count") else "") + {
            "day": "daily values", "month": "monthly values", "year": "yearly values", "snow_season": "snow-season totals" if series["metric"] == "SNOW" else "values per Jul-Jun season",
            "season": f"{series['season']} values, one per year", "month_of_year": "averages for each calendar month",
            "season_of_year": "averages for each season",
        }[series["unit"]],
        "metric": series["metric"],
        "what": series["metric_label"],
        "station_name": series["station_name"],
        "place": series["place"],
        "units": series["units"],
        "points": len(pts),
        "notes": series["notes"],
    }
    if not pts:
        out["value"] = None
        return out
    hi = max(pts, key=lambda p: p["value"])
    lo = min(pts, key=lambda p: p["value"])
    out.update({
        "period": f"{pts[0]['label']} to {pts[-1]['label']}",
        "highest": {"value": hi["value"], "when": hi["label"]},
        "lowest": {"value": lo["value"], "when": lo["label"]},
        "mean": _r(float(np.mean([p["value"] for p in pts]))),
    })
    if series["unit"] == "day" and series["metric"] in _TOTALS and not series.get("count"):
        del out["lowest"]  # a dry day: 0, every time
    if series["baseline"]:
        out["normals_period"] = series["baseline"]
    anomalies = [p for p in pts if p.get("anomaly") is not None]
    if anomalies:
        above = max(anomalies, key=lambda p: p["anomaly"])
        below = min(anomalies, key=lambda p: p["anomaly"])
        out["mean_departure_from_normal"] = _r(float(np.mean([p["anomaly"] for p in anomalies])))
        out["furthest_above_normal"] = {"departure": above["anomaly"], "when": above["label"]}
        out["furthest_below_normal"] = {"departure": below["anomaly"], "when": below["label"]}
    if series["trend"]:
        out["trend_per_decade"] = series["trend"]["per_decade"]
    return out
