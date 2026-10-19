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
from datetime import date, timedelta

import numpy as np

import pipeline

CLIMATOLOGY_UNITS = {"month_of_year", "season_of_year", "day_of_year", "week_of_year"}
# daily histogram bin widths in the data's units (temperatures: 2°F, 1°C)
HISTOGRAM_BIN = {"PRCP": 0.1, "SNOW": 1.0, "SNWD": 1.0, "AWND": 2.0, "HDD": 2.0, "CDD": 2.0}
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
        **{k: entry[k] for k in ("period_aggregation", "threshold") if k in entry},
        "start_date": f"{sy}-01-01",
        "end_date": f"{ey}-12-31",
    }
    unit = entry["unit"]
    if unit == "day":
        return {**base, "unit": "day_of_year"}
    if unit == "month":
        return {**base, "unit": "month_of_year"}
    if unit == "season":
        return {**base, "unit": "season_of_year"}
    if unit == "snow_season":
        # seasons ending in sy..ey (labeled by the year they end in)
        return {**base, "unit": "snow_season", "start_date": f"{sy - 1}-07-01", "end_date": f"{ey}-06-30"}
    return {**base, "unit": unit}


def records_entry(entry: dict) -> dict | None:
    """The whole-record day_of_year query a climate graph's record envelope needs."""
    if entry.get("display") != "climate":
        return None
    return {
        "station_id": entry["station_id"], "metric": entry["metric"], "aggregation": "series",
        "unit": "day_of_year", "start_date": "1800-01-01", "end_date": date.today().isoformat(),
    }


def _aligned_x(iso: str, start: date) -> str:
    """
    The date moved into a reference season that starts in the same month as
    the series, so several seasons (2009-10, 2015-16) line up day for day.
    The reference year has its Feb 29 (2000, or 1999-2000 for a July start).
    """
    d = date.fromisoformat(iso)
    ref = 2000 if start.month <= 2 else 1999
    year = ref + (1 if (d.month, d.day) < (start.month, start.day) else 0)
    return date(year, d.month, d.day).isoformat()


def _period_label(entry: dict) -> str:
    """'2009-10' for a Jul-Jun season, '2024' for one calendar year, else the years."""
    s, e = date.fromisoformat(entry["start_date"]), date.fromisoformat(entry["end_date"])
    if s.year == e.year:
        return str(s.year)
    return f"{s.year}-{e.year % 100:02d}"


def _convert(entry: dict, temp_unit: str):
    """(value converter, spread converter) for the entry's output unit."""
    to_c = (
        not _is_count(entry) and not _is_odds(entry)
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


def _is_dates(entry: dict) -> bool:
    """A series of the first or last date each year a threshold is met (freeze dates)."""
    return entry.get("period_aggregation") in ("first_date", "last_date")


def _dates_label(entry: dict, temp_unit: str) -> str:
    """'Last spring freeze', 'First fall freeze', or 'First day with high ≥ 90°F'."""
    t = entry["threshold"]
    first = entry["period_aggregation"] == "first_date"
    months = set(entry.get("month_filter") or [])
    half = " spring" if months and months <= set(range(1, 7)) else " fall" if months and months <= set(range(7, 13)) else ""
    if entry["metric"] == "TMIN" and t["operator"] in ("<", "<=") and t["value"] == 32:
        return f"{'First' if first else 'Last'}{half} freeze"
    return f"{'First' if first else 'Last'}{half} " + _count_label(entry, temp_unit).replace("Days with", "day with")


def _ref_date(iso: str, snow_season: bool) -> date:
    """The date moved onto a reference year (2000; 1999-2000 for a Jul-Jun season) so years share one axis."""
    d = date.fromisoformat(iso)
    return date(1999 if snow_season and d.month >= 7 else 2000, d.month, d.day)


def _is_odds(entry: dict) -> bool:
    """A climatology of the share of years with at least one day meeting a threshold."""
    return entry.get("period_aggregation") == "any"


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
    if _is_dates(entry):
        return "date"
    if _is_odds(entry):
        return "% of years"
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
    if entry["unit"] == "day_of_year":
        # smoothed normals on a leap-year axis (2000), records alongside
        days = result.get("days", [])
        values = pipeline.smooth_day_of_year(result.get("values", []))
        spread = pipeline.smooth_day_of_year(result.get("stddev", []))
        return [
            {"x": f"2000-{d}", "label": f"{calendar.month_abbr[int(d[:2])]} {int(d[3:])}",
             "value": v, "stddev": sd, "n_years": n,
             "record_high": hi, "record_high_year": hy, "record_low": lo, "record_low_year": ly}
            for d, v, sd, n, hi, hy, lo, ly in zip(
                days, values, spread, result.get("n_years", []), result.get("max", []),
                result.get("max_year", []), result.get("min", []), result.get("min_year", []),
            )
        ]
    if entry["unit"] == "week_of_year":
        # 7-day weeks from Jan 1, drawn on a 2000 axis, labeled by their first day
        out = []
        for w, v, sd, n in zip(result.get("weeks", []), result.get("values", []), result.get("stddev", []), result.get("n_years", [])):
            start = date(2000, 1, 1) + timedelta(days=7 * (w - 1))
            out.append({"x": start.isoformat(), "x_end": (start + timedelta(days=7)).isoformat(),
                        "label": f"week of {calendar.month_abbr[start.month]} {start.day}", "value": v, "stddev": sd, "n_years": n})
        return out
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
    if unit == "day":
        days = base_result.get("days", [])
        normals = pipeline.smooth_day_of_year(base_result.get("values", []))
        spreads = pipeline.smooth_day_of_year(base_result.get("stddev", []))
        for d, v, sd, n in zip(days, normals, spreads, base_result.get("n_years", [])):
            # Feb 29 has a quarter of the years; its smoothed normal is still sound
            if n >= need or d == "02-29":
                out[d] = (v, sd)
            else:
                dropped += 1
    elif unit == "month":
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


def _nice_width(raw: float) -> float:
    """A bin width of 1, 2, or 5 times a power of ten, at least raw."""
    if raw <= 0:
        return 1.0
    mag = 10 ** np.floor(np.log10(raw))
    return float(next(m * mag for m in (1, 2, 5, 10) if m * mag >= raw))


def _histogram(pairs: list[tuple[float, int]], width: float, units: str) -> tuple[list[dict], dict]:
    """
    (value, count) pairs binned into [lo, lo + width) bins, plus the median,
    10th/90th percentiles, and count for the answer. Empty bins between the
    first and last are kept so the shape reads true.
    """
    if not pairs:
        return [], {"n": 0}
    digits = 2 if width < 1 else 0 if float(width).is_integer() else 1
    lo0 = np.floor(min(v for v, _ in pairs) / width) * width
    bins: dict[int, int] = {}
    for v, c in pairs:
        bins[int(np.floor((v - lo0) / width + 1e-9))] = bins.get(int(np.floor((v - lo0) / width + 1e-9)), 0) + c
    total = sum(bins.values())
    unit = units if units.startswith("°") else f" {units}"
    fmt = lambda x: f"{round(x, digits):g}" if digits else f"{int(round(x))}"
    points = []
    for k in range(max(bins) + 1):
        lo, hi = lo0 + k * width, lo0 + (k + 1) * width
        n = bins.get(k, 0)
        points.append({"x": round(lo, 2), "x_end": round(hi, 2), "label": f"{fmt(lo)}-{fmt(hi)}{unit}",
                       "value": n, "pct": round(100 * n / total, 1)})
    flat = np.repeat([v for v, _ in pairs], [c for _, c in pairs])
    r = lambda x: round(float(x), 2 if width < 1 else 1)
    stats = {"n": int(total), "median": r(np.median(flat)), "p10": r(np.percentile(flat, 10)), "p90": r(np.percentile(flat, 90)),
             "most_common": max(points, key=lambda p: p["value"])["label"]}
    return points, stats


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
    records_result: dict | None = None,
) -> dict:
    """One chart line, JSON-safe so it can live in the session history."""
    station = station_info.get(entry["station_id"], {})
    unit = entry["unit"]
    climatology = unit in CLIMATOLOGY_UNITS
    kind = (
        "dates" if _is_dates(entry)
        else "climograph" if entry.get("display") == "climograph"
        else "histogram" if unit == "value" or entry.get("display") == "histogram"
        else "climatology" if climatology
        else entry["display"] if entry.get("display") in ("climate", "cumulative")
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
            _dates_label(entry, temp_unit) if _is_dates(entry)
            else _count_label(entry, temp_unit) if _is_count(entry)
            else _count_label(entry, temp_unit).replace("Days with", "Chance of a day with") if _is_odds(entry)
            else pipeline._METRIC_LABELS.get(entry["metric"], entry["metric"])
        ),
        "count": _is_count(entry),
        "odds": _is_odds(entry),
        "month_filter": entry.get("month_filter"),
        "station_id": entry["station_id"],
        "place": station.get("city") or station.get("name") or entry["station_id"],
        "station_name": station.get("name") or entry["station_id"],
        "units": _units(entry, temp_unit),
        # totals and day counts are amounts per bucket: bars, from zero
        "bars": _is_count(entry) or _is_odds(entry) or unit == "value" or (
            entry["metric"] in _TOTALS and (entry.get("period_aggregation") == "sum" or unit == "day")
        ),
        "baseline": (
            f"{entry['baseline']['start_year']}-{entry['baseline']['end_year']}" if entry.get("baseline") else None
        ),
        "season": entry.get("season"),
        "period": _period_label(entry) if unit == "day" else None,
        "display": entry.get("display"),
        "points": [],
        "trend": None,
        "notes": notes,
    }
    if not result or result.get("error"):
        return series

    if kind == "dates":
        snow = unit == "snow_season"
        field = "first" if entry["period_aggregation"] == "first_date" else "last"
        points, missing = [], 0
        for y, d in zip(result.get("years", []), result.get(field, [])):
            if not d:
                missing += 1
                continue
            ref = _ref_date(d, snow)
            points.append({"x": f"{y}-01-01", "x_num": float(y), "label": pipeline.season_label(entry, y) or str(y),
                           "year": y, "date": d, "y_ref": ref.isoformat(), "value": pipeline.season_day(d, snow),
                           "when": date.fromisoformat(d).strftime("%b %-d, %Y")})
        if missing:
            notes.append(f"{missing} year(s) had no such day.")
        series["points"] = points
        if entry.get("trend") and (t := _trend(points, "value")):
            origin = date(1999, 7, 1) if snow else date(2000, 1, 1)
            for end in ("start", "end"):
                t[end]["y_ref"] = (origin + timedelta(days=round(t[end]["value"]))).isoformat()
            series["trend"] = t
        return series

    if unit == "value":
        pairs = [(conv(v), c) for v, c in zip(result.get("values", []), result.get("counts", []))]
        if entry["metric"] in _TOTALS | {"SNWD"}:
            pairs = [(v, c) for v, c in pairs if v > 0]
            notes.append("Days with none are left out.")
        width = HISTOGRAM_BIN.get(entry["metric"], 1.0 if series["units"] == "°C" else 2.0)
        series["points"], series["hist_stats"] = _histogram(pairs, width, series["units"])
        return series

    if climatology:  # (a climograph's parts are climatologies too)
        points = _climatology_points(entry, result)
        for p in points:
            if _is_odds(entry):
                # a share of years: a percent, with no spread band
                p["value"] = round(p["value"] * 100, 1) if p["value"] is not None else None
                p["stddev"] = None
                continue
            p["value"] = _round(conv(p["value"])) if p["value"] is not None else None
            for f in ("record_high", "record_low"):
                if p.get(f) is not None:
                    p[f] = _round(conv(p[f]))
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

    if kind == "histogram":
        # the spread of the period values (annual snowfall, summer means)
        vals = [p["value"] for p in points if p["value"] is not None]
        span = (max(vals) - min(vals)) if vals else 0
        series["points"], series["hist_stats"] = _histogram([(v, 1) for v in vals], _nice_width(span / 10), series["units"])
        series["period_count"] = len(vals)
        return series

    normals = _normals(entry, base_result, notes) if entry.get("baseline") else None
    if entry.get("baseline"):
        for p in points:
            key = p["x"][5:] if unit == "day" else p["month"] if unit == "month" else None
            if normals and key in normals:
                normal, sd = normals[key]
                p["normal"] = _round(conv(normal))
                sd = spread(sd) if sd is not None else None
                p["lower"] = _round(p["normal"] - sd) if sd is not None else None
                p["upper"] = _round(p["normal"] + sd) if sd is not None else None
                p["anomaly"] = _round(p["value"] - p["normal"])

    if kind == "climate" and records_result and not records_result.get("error"):
        recs = {
            d: (hi, hy, lo, ly)
            for d, hi, hy, lo, ly in zip(records_result.get("days", []), records_result.get("max", []),
                                         records_result.get("max_year", []), records_result.get("min", []),
                                         records_result.get("min_year", []))
        }
        for p in points:
            if (r := recs.get(p["x"][5:])) is None:
                continue
            p["record_high"], p["record_high_year"] = _round(conv(r[0])), r[1]
            p["record_low"], p["record_low_year"] = _round(conv(r[2])), r[3]
            # the whole record includes this period: a tie or a new record
            if entry["metric"] == "TMAX" and p["value"] >= p["record_high"]:
                p["record_set"] = "high"
            elif entry["metric"] == "TMIN" and p["value"] <= p["record_low"]:
                p["record_set"] = "low"

    if kind == "cumulative":
        start = date.fromisoformat(entry["start_date"])
        total = normal_total = 0.0
        for p in points:
            p["daily"] = p["value"]
            total += p["value"] or 0
            p["value"] = _round(total)
            if p.get("normal") is not None:
                normal_total += p["normal"]
                p["normal"] = _round(normal_total)
                p["anomaly"] = _round(p["value"] - p["normal"])
            p.pop("lower", None), p.pop("upper", None)
            p["x_aligned"] = _aligned_x(p["x"], start)

    if entry.get("trend"):
        series["trend"] = _trend(points, "anomaly" if kind == "anomaly" else "value")
    series["points"] = points
    return series


TREND_CAVEAT = "Trend lines use the station's raw record, so they include station changes (instrument swaps, site moves) as well as climate."


def _difference(a: dict, b: dict) -> dict:
    """Two stations' series as one: a minus b at each shared point."""
    bv = {p["x"]: p["value"] for p in b["points"]}
    pts = [{**p, "value": round(p["value"] - bv[p["x"]], 2)} for p in a["points"]
           if p["value"] is not None and bv.get(p["x"]) is not None]
    for p in pts:
        for k in ("normal", "lower", "upper", "anomaly", "stddev", "record_high", "record_low"):
            p.pop(k, None)
    return {**a, "kind": "difference", "points": pts, "trend": None, "baseline": None,
            "name": f"{a['place']} minus {b['place']}", "minus_place": b["place"]}


def _growing_season(a: dict, b: dict) -> dict:
    """Days between the last spring freeze and the first fall freeze, per year."""
    spring, fall = (a, b) if a["metric_label"].startswith("Last") else (b, a)
    fall_by_year = {p["year"]: p["date"] for p in fall["points"]}
    pts = []
    for p in spring["points"]:
        if p["year"] in fall_by_year:
            days = (date.fromisoformat(fall_by_year[p["year"]]) - date.fromisoformat(p["date"])).days
            pts.append({"x": p["x"], "x_num": p["x_num"], "label": p["label"], "year": p["year"], "value": days})
    out = {**spring, "kind": "time_series", "units": "days", "metric_label": "Growing season (days between freezes)",
           "points": pts, "display": None, "bars": False}
    out["trend"] = _trend(pts, "value") if (spring.get("trend") or fall.get("trend")) else None
    return out


def _climograph_chart(members: list[dict]) -> dict:
    """Monthly temperatures (lines) and precipitation (bars) in one two-axis chart."""
    for m in members:
        m["name"] = m["metric_label"]
    place = members[0]["place"]
    period = members[0]["baseline"]
    return {"kind": "climograph", "unit": "month_of_year", "units": members[0]["units"],
            "title": f"Climograph · {place}", "heading": "Climograph" + (f" ({period})" if period else ""),
            "subtitle": place, "series": members,
            "notes": list(dict.fromkeys(n for m in members for n in m["notes"])) + ["Click a legend item to hide or show it."]}


def group_charts(series_list: list[dict]) -> list[dict]:
    """
    Series that share a chart kind, grain, and unit of measure go on one
    chart (e.g. high and low together); anything else gets its own chart.
    A climograph's parts share one two-axis chart; a difference pair becomes
    one A - B series.
    """
    charts = []
    clim = [s for s in series_list if s["kind"] == "climograph" and s["points"]]
    if clim:
        charts.append(_climograph_chart(clim))
    series_list = [s for s in series_list if s["kind"] != "climograph"]
    grow = [s for s in series_list if s.get("display") == "growing_season" and s["points"]]
    if len(grow) == 2:
        series_list = [s for s in series_list if s.get("display") != "growing_season"] + [_growing_season(*grow)]
    diff = [s for s in series_list if s.get("display") == "difference" and s["points"]]
    if len(diff) == 2:
        series_list = [s for s in series_list if s.get("display") != "difference"] + [_difference(*diff)]

    groups: dict[tuple, list[dict]] = {}
    for s in series_list:
        if s["points"]:
            groups.setdefault((s["kind"], s["unit"], s["units"], s["season"]), []).append(s)

    for (kind, unit, units, _), members in groups.items():
        places = list(dict.fromkeys(m["place"] for m in members))
        metrics = list(dict.fromkeys(m["metric_label"] for m in members))
        periods = list(dict.fromkeys(m.get("period") for m in members))
        for m in members:
            if m["kind"] == "difference":
                continue  # named in _difference
            # name each line by what differs between the lines
            parts = ([m["metric_label"]] if len(metrics) > 1 else []) + ([m["place"]] if len(places) > 1 else [])
            if len(periods) > 1:
                parts.append(m["period"])
            m["name"] = " · ".join(parts) or m["metric_label"]
        what = " & ".join(metrics)
        grain = {
            "day": "daily", "month": "monthly", "year": "yearly", "value": "daily", "week_of_year": "by week", "season": f"{members[0]['season'] or ''}s",
            "snow_season": "by snow season" if members[0]["metric"] == "SNOW" else "by cold season (Jul-Jun)",
            "month_of_year": "by month", "season_of_year": "by season", "day_of_year": "by day of year",
        }[unit]
        period = members[0]["baseline"]
        if kind == "climatology":
            heading = f"{what} — {({'month_of_year': 'monthly', 'season_of_year': 'seasonal', 'week_of_year': 'weekly'}).get(unit, 'daily')} climatology"
            if members[0].get("odds"):
                heading = f"{what} — {({'month_of_year': 'by month', 'season_of_year': 'by season', 'week_of_year': 'by week'}).get(unit, 'by day')}"
            heading += f" ({period})" if period else ""
        elif kind == "overlay":
            heading = f"{what}, {grain} vs. {period} normal"
        elif kind == "anomaly":
            heading = f"{what}, {grain} departure from {period} normal"
        elif kind == "histogram":
            mf = members[0].get("month_filter")
            months = " in " + "/".join(calendar.month_name[m] for m in mf) if mf else ""
            per = {"year": "yearly", "snow_season": "snow-season", "month": "monthly", "season": members[0]["season"] or "seasonal"}
            kind_of = "totals" if members[0]["bars"] else "averages"
            heading = f"{what}, distribution of {'daily values' if unit == 'value' else per.get(unit, '') + ' ' + kind_of}{months}"
        elif kind == "difference":
            heading = f"{what}, {grain}: {members[0]['place']} minus {members[0]['minus_place']}"
        elif kind == "climate":
            heading = f"{what}, daily vs. {period} normals and records"
        elif kind == "cumulative":
            heading = f"{what}, running total" + (f" vs. {period} normal" if period else "")
        else:
            heading = f"{what}, {grain}"
        notes = list(dict.fromkeys(n for m in members for n in m["notes"]))
        if len(members) > 1 or kind == "climate":
            notes.insert(0, "Click a legend item to hide or show it.")
        if any(m.get("trend") for m in members):
            notes.append(TREND_CAVEAT)
        if kind in ("overlay", "anomaly", "climate") or (kind in ("climatology", "cumulative") and members[0]["baseline"]):
            notes.append(
                "Normals are computed from this station's observations and may differ slightly from NOAA's official normals."
            )
        # Subtitle: where (one place) and when (first to last point of a time series)
        sub = [places[0]] if len(places) == 1 else []
        if kind not in ("climatology", "histogram") and len(periods) == 1:  # several periods: the legend names them
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
        if kind == "climatology" and unit not in ("day_of_year", "week_of_year"):  # too many rows for a table
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
            "day": "daily values", "month": "monthly values", "year": "yearly values", "value": "daily values",
            "week_of_year": "values for each week of the year", "snow_season": "snow-season totals" if series["metric"] == "SNOW" else "values per Jul-Jun season",
            "season": f"{series['season']} values, one per year", "month_of_year": "averages for each calendar month",
            "season_of_year": "averages for each season",
            "day_of_year": "averages for each calendar day, smoothed over +/- 7 days",
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
    if series["kind"] == "cumulative":
        # a running total: the end is what matters, not its highest/lowest
        for k in ("highest", "lowest", "mean", "mean_departure_from_normal", "furthest_above_normal", "furthest_below_normal"):
            out.pop(k, None)
        out["grain"] = "running total of daily values"
        out["total"] = {"value": pts[-1]["value"], "through": pts[-1]["label"]}
        if pts[-1].get("normal") is not None:
            out["normal_total_to_date"] = pts[-1]["normal"]
            out["departure_from_normal"] = pts[-1]["anomaly"]
        out["period_label"] = series.get("period")
    if series.get("display") == "difference":
        out["chart"] = "one line: the first station's values minus the second's, around zero"
    if series["kind"] == "dates":
        # dates: earliest/latest by time of year, not highest/lowest numbers
        for k in ("highest", "lowest", "mean"):
            out.pop(k, None)
        if pts:
            by_day = sorted(pts, key=lambda p: p["value"])
            tied = lambda end: [p["when"] for p in pts if p["value"] == end["value"]]  # ties reported, not broken
            out.update(grain="the date each year", what=series["metric_label"],
                       earliest={"dates": tied(by_day[0])}, latest={"dates": tied(by_day[-1])},
                       median_date=_ref_date(by_day[len(by_day) // 2]["date"], series["unit"] == "snow_season").strftime("%B %-d"))
        if series["trend"]:
            out["trend_days_per_decade"] = series["trend"]["per_decade"]
            out["trend_meaning"] = "negative = earlier over time"
            out.pop("trend_per_decade", None)
    if series["kind"] == "histogram":
        # a distribution: its shape and middle, not a highest/lowest over time
        for k in ("highest", "lowest", "mean", "period"):
            out.pop(k, None)
        out["grain"] = "histogram of " + ("daily values" if series["unit"] == "value" else "per-period values")
        out["distribution"] = series.get("hist_stats")
    if series.get("odds"):
        out["grain"] = "percent of years with at least one day meeting the condition"
        out.pop("mean", None)  # an average of odds across months means nothing
        if len(pts) <= 12:  # every month/season, so none is guessed at
            out["by_period"] = {p["label"]: p["value"] for p in pts}
    if series["kind"] == "climate":
        out["records_set_or_tied"] = [f"{p['label']} ({p['record_set']})" for p in pts if p.get("record_set")]
    return out
