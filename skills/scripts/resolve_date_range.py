#!/usr/bin/env python3
"""Deterministic date-range resolver for the weather-query-resolver skill.

Takes a structured time expression -- already classified by the model from
the user's natural-language question -- and returns {start_date, end_date}
in YYYY-MM-DD, doing the calendar arithmetic (leap years, month lengths,
season/year boundaries) so the model doesn't have to.

Usage:
    python resolve_date_range.py '<json>'

Input JSON shapes (current_date is always required, YYYY-MM-DD):

  {"current_date": "2026-08-04", "kind": "absolute_year", "year": 2021}
  {"current_date": "2026-08-04", "kind": "absolute_year_range", "start_year": 2000, "end_year": 2020}
  {"current_date": "2026-08-04", "kind": "absolute_month", "year": 2022, "month": 12}
  {"current_date": "2026-08-04", "kind": "absolute_day", "year": 2026, "month": 1, "day": 25}
  {"current_date": "2026-08-04", "kind": "season", "season": "winter"}
  {"current_date": "2026-08-04", "kind": "season", "season": "winter", "year": 2026}
  {"current_date": "2026-08-04", "kind": "relative_days", "days": 30}
  {"current_date": "2026-08-04", "kind": "last_years", "years": 5}
  {"current_date": "2026-08-04", "kind": "weekday", "weekday": "friday"}
  {"current_date": "2026-08-04", "kind": "recent_month", "month": 1}
  {"current_date": "2026-08-04", "kind": "all_time"}

For "season" with no "year", resolves to the most recently completed
instance of that season as of current_date. With "year", "year" is the
calendar year containing the season's last month (so winter year=2026 means
Dec 2025 - Feb 2026).

For "last_years" ("the last 5 years"), resolves to Jan 1 of the earliest of
the N most recent calendar years through current_date: the current year
counts as one of them though it isn't over (asked in August 2026, 5 years is
2022-01-01 to 2026-08-04).

For "weekday" ("last Friday", "this past Friday", "on Friday"), resolves to
the single most recent such day strictly before current_date -- so asked on
a Saturday, "last Friday" is yesterday; asked on a Friday, it's a week ago.

Output on stdout: {"start_date": "...", "end_date": "..."}
On invalid input: {"error": "..."} and a non-zero exit code.
"""
import sys
import json
import calendar
from datetime import date, timedelta


# ---------------------------------------------------------------------------
# Constants
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


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------


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

    if kind == "last_years":
        # "the last 5 years": the N most recent calendar years, this one
        # included though it isn't over, through today
        years = int(payload["years"])
        if years < 1:
            raise ValueError(f"years must be at least 1, got {years}")
        return date(current.year - years + 1, 1, 1), current

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


def main():
    if len(sys.argv) != 2:
        print(json.dumps({"error": "expected exactly one JSON argument"}))
        sys.exit(1)
    try:
        payload = json.loads(sys.argv[1])
        start, end = resolve(payload)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}))
        sys.exit(1)
    print(json.dumps({"start_date": start.isoformat(), "end_date": end.isoformat()}))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
