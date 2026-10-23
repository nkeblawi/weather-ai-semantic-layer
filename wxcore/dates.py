"""
Seasons, date labels, day-of-year smoothing, and resolve_date_range.

resolve_date_range's logic is a copy of skills/scripts/resolve_date_range.py
(the skill keeps its own for standalone use). This copy is the one the app
and the MCP server run; a parity test keeps the two in step.
"""

import calendar
from datetime import date, timedelta

SNOW_SEASON_START_MONTH = 7  # NWS snow season: July 1 - June 30
# halves of the year for first/last dates: the last spring freeze, the first fall freeze
SPRING_MONTHS = list(range(1, 7))
FALL_MONTHS = list(range(7, 13))
# "normal": the current 30-year climate normals period
NORMAL_START_YEAR, NORMAL_END_YEAR = 1991, 2020

# Meteorological seasons (SEASON_MONTHS, below). Winter's year starts in
# December so Dec-Feb stays one bucket, labeled by the year it ends in
# (Dec 2009 - Feb 2010 = 2010).
SEASON_START_MONTH = {"winter": 12}

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


def snow_season_label(bucket_year: int) -> str:
    """A snow season bucket is labeled by the year it ends in: 2010 -> "2009-10"."""
    return f"{bucket_year - 1}-{bucket_year % 100:02d}"


def season_label(entry: dict, bucket_year: int, sentence: bool = False) -> str | None:
    """
    How a snow_season/season bucket reads ("2009-10", "Winter 2009-10",
    "Summer 2024"), so it's never mistaken for a calendar year; None for any
    other unit. sentence=True lowercases the season name ("summer 2024") for
    text the answer model writes mid-sentence; tables and charts keep the
    capital.
    """
    unit = entry.get("unit")
    if unit == "snow_season":
        return snow_season_label(bucket_year)
    if unit == "season":
        season = entry["season"]
        span = snow_season_label(bucket_year) if season == "winter" else str(bucket_year)
        return f"{season if sentence else season.capitalize()} {span}"
    return None


# ---------------------------------------------------------------------------
# resolve_date_range (copied from skills/scripts/resolve_date_range.py)
# ---------------------------------------------------------------------------

ALL_TIME_START = "1800-01-01"

# (start_month, end_month) of each season; winter wraps into the prior year.
SEASON_MONTHS = {
    "winter": (12, 2),
    "spring": (3, 5),
    "summer": (6, 8),
    "fall": (9, 11),
    "autumn": (9, 11),
}

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _last_day(year, month):
    return calendar.monthrange(year, month)[1]


def _season_range(season, end_year):
    season = season.lower()
    if season not in SEASON_MONTHS:
        raise ValueError(f"unknown season: {season}")
    start_month, end_month = SEASON_MONTHS[season]
    start_year = end_year - 1 if season == "winter" else end_year
    start = date(start_year, start_month, 1)
    end = date(end_year, end_month, _last_day(end_year, end_month))
    return start, end


def _most_recent_completed_season(season, current):
    for end_year in (current.year, current.year - 1, current.year - 2):
        start, end = _season_range(season, end_year)
        if end <= current:
            return start, end
    raise ValueError("could not resolve most recently completed season")


def resolve(payload):
    current = date.fromisoformat(payload["current_date"])
    kind = payload["kind"]

    if kind == "absolute_year":
        year = int(payload["year"])
        return date(year, 1, 1), date(year, 12, 31)

    if kind == "absolute_year_range":
        start_year, end_year = int(payload["start_year"]), int(payload["end_year"])
        return date(start_year, 1, 1), date(end_year, 12, 31)

    if kind == "absolute_month":
        year, month = int(payload["year"]), int(payload["month"])
        return date(year, month, 1), date(year, month, _last_day(year, month))

    if kind == "absolute_day":
        d = date(int(payload["year"]), int(payload["month"]), int(payload["day"]))
        return d, d

    if kind == "season":
        season = payload["season"]
        if payload.get("year") is not None:
            return _season_range(season, int(payload["year"]))
        return _most_recent_completed_season(season, current)

    if kind == "relative_days":
        days = int(payload["days"])
        return current - timedelta(days=days), current

    if kind == "recent_month":
        # Most recent instance of that month that has started: asked in
        # October, "January" is this year's January; "November" is last
        # year's. The current month resolves to itself (its end may be in
        # the future; observations simply stop at today).
        month = int(payload["month"])
        if not 1 <= month <= 12:
            raise ValueError(f"month must be 1-12, got {month}")
        year = current.year if month <= current.month else current.year - 1
        return date(year, month, 1), date(year, month, _last_day(year, month))

    if kind == "weekday":
        weekday = payload["weekday"].lower()
        if weekday not in WEEKDAYS:
            raise ValueError(f"unknown weekday: {weekday}")
        # 1..7 days back, never 0: today is not "last <today's weekday>"
        days_back = (current.weekday() - WEEKDAYS.index(weekday) - 1) % 7 + 1
        d = current - timedelta(days=days_back)
        return d, d

    if kind == "all_time":
        return date.fromisoformat(ALL_TIME_START), current

    raise ValueError(f"unknown kind: {kind}")


def season_months(season: str) -> list[int]:
    """Every month of a season in order, from SEASON_MONTHS: winter -> [12, 1, 2]."""
    start, end = SEASON_MONTHS[season]
    return [(start - 1 + i) % 12 + 1 for i in range((end - start) % 12 + 1)]


def resolve_date_range(payload: dict) -> dict:
    """
    resolve()'s (start, end) date tuple as the {"start_date", "end_date"}
    strings a tool result returns.
    """
    start, end = resolve(payload)
    return {"start_date": start.isoformat(), "end_date": end.isoformat()}
