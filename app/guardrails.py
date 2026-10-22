"""
Guardrails for the public, no-sign-in chat: the usage limits and when a
session has hit one (session_limit), cheap checks that run before a question
reaches Claude (pre_check), and deterministic validation of what the
resolver returns before anything runs on the warehouse (validate_resolved).

Nothing here calls a model or the database. Blocked messages are never sent
to Claude and never written to chat_history (a write is warehouse compute
too); callers log them instead.

The allowed-value sets below mirror SKILL.md Step 6's output schema;
tests/test_guardrails.py (local-only, gitignored) fails if the two drift apart.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

# Usage caps. They mainly protect the Databricks Free Edition daily compute
# quota, which the ingest jobs share. Question caps count only resolved
# questions (ones that ran answer_query); clarifications and errors are free.
# /clear doesn't reset any of them.
SESSION_QUESTION_LIMIT = int(os.environ.get("WX_SESSION_QUESTION_LIMIT", "20"))
DAILY_QUESTION_LIMIT = int(os.environ.get("WX_DAILY_QUESTION_LIMIT", "100"))

# Backstop on every turn that reaches Claude (clarifications and errors
# included), so free clarifications can't run up unlimited model calls.
SESSION_TURN_LIMIT = int(os.environ.get("WX_SESSION_TURN_LIMIT", "30"))
MAX_QUESTION_CHARS = int(os.environ.get("WX_MAX_QUESTION_CHARS", "500"))

# SKILL.md asks for under 300; real unknown-city clarifications still run to
# ~420, so the hard cap leaves headroom while bounding how much model text a
# manipulated reply can show.
MAX_CLARIFICATION_CHARS = 500
OFF_TOPIC_STREAK_LIMIT = 3  # in a row -> session ends
RATE_LIMIT_MESSAGES = 5
RATE_LIMIT_WINDOW_SECS = 60
MAX_QUERIES_PER_QUESTION = 4  # each query is a warehouse call
MAX_RANK_LIMIT = 15  # top/bottom ranks per query; answer_query clamps to the same

# /plot: a monthly series longer than this becomes yearly points, which keeps
# 4 series under Altair's 5,000-row default (4 x 1,200 points)
MAX_MONTHLY_SERIES_YEARS = 100

# /plot: daily points only for a span this long or shorter (a year, leap or
# not, or one snow season); longer daily requests become monthly
MAX_DAILY_SERIES_DAYS = 366
MIN_BASELINE_YEARS, MAX_BASELINE_YEARS = 10, 50

# ---------------------------------------------------------------------------
# Allowed values (SKILL.md Step 6, plus references/plotting.md for series)
# ---------------------------------------------------------------------------

METRICS = {"TAVG", "TMAX", "TMIN", "PRCP", "SNOW", "SNWD", "AWND", "HDD", "CDD"}
AGGREGATIONS = {
    "mean",
    "median",
    "sum",
    "max",
    "min",
    "stddev",
    "count",
    "latest",
    "earliest",
    "top",
    "bottom",
    "series",
    "first_date",
    "last_date",
}
# the first/last day each year (or Jul-Jun season) meeting a threshold: freeze dates, first 90-degree day
DATE_AGGREGATIONS = {"first_date", "last_date"}
DATE_UNITS = {"year", "snow_season"}
RANKING_AGGREGATIONS = {"top", "bottom"}
UNITS = {
    "day",
    "event",
    "month",
    "year",
    "snow_season",
    "season",
    "month_of_year",
    "season_of_year",
    "day_of_year",
    "week_of_year",
    "value",
}
CLIMATOLOGY_UNITS = {
    "month_of_year",
    "season_of_year",
    "day_of_year",
    "week_of_year",
}  # series only, except day_of_year (below)
# day_of_year outside a series: one calendar day's record (max/min) or normal (mean)
CALENDAR_DAY_AGGREGATIONS = {"max", "min", "mean"}
SERIES_UNITS = {
    "day",
    "month",
    "year",
    "season",
    "snow_season",
    "value",
} | CLIMATOLOGY_UNITS
DISPLAYS = {
    "overlay",
    "anomaly",
    "climate",
    "cumulative",
    "histogram",
    "difference",
    "climograph",
    "growing_season",
}
# difference: two stations' series as one A - B line; climograph: monthly temps + precip in one chart
CLIMOGRAPH_METRICS = {"TMAX", "TMIN", "TAVG", "PRCP", "SNOW"}
# histogram of per-period values (the spread of annual snowfall); unit value is always one
HISTOGRAM_UNITS = {"month", "year", "season", "snow_season"}
# climate: NWS-style daily graph (normals + records); cumulative: running total
DAILY_DISPLAYS = {"climate": {"TMAX", "TMIN", "TAVG"}, "cumulative": {"PRCP", "SNOW"}}
CLARIFICATION_REASONS = {"needs_plot"}
SEASONS = {"winter", "spring", "summer", "fall"}
OPERATORS = {">", ">=", "<", "<=", "="}
EVENT_METRICS = {"PRCP", "SNOW"}
EVENT_ONLY_AGGREGATIONS = {"latest", "earliest"}
EVENT_VALUES = {"total", "duration"}
PERIOD_AGGREGATIONS = {"mean", "sum"}
# a series bucket can also count the days meeting its threshold (90F+ days per year)
SERIES_PERIOD_AGGREGATIONS = PERIOD_AGGREGATIONS | {"count", "any"} | DATE_AGGREGATIONS
# any: 1 if a day in the bucket meets the threshold -- a climatology of it is the share of years
OUTPUT_UNITS = {"C", "F"}
STATUSES = {"resolved", "needs_clarification", "off_topic"}

# Plot modes (see chat_session.get_answer): an explicit /plot turn must chart
# or ask; a follow-up to a chart may chart or answer normally; any other turn
# may not chart.
PLOT_EXPLICIT, PLOT_INHERITED, PLOT_OFF = "plot", "plot_inherited", None
MIN_DATE = date(1800, 1, 1)

# ---------------------------------------------------------------------------
# Canned replies (shown instead of model-written text)
# ---------------------------------------------------------------------------

OFF_TOPIC_MSG = (
    "I can only answer questions about past weather at the stations in the "
    "Home city list. Type /help for examples."
)
SESSION_ENDED_MSG = (
    "This session has ended after several off-topic messages. "
    "Thanks for trying the app!"
)
TOO_LONG_MSG = f"Please keep questions under {MAX_QUESTION_CHARS} characters."
SLOW_DOWN_MSG = "You're sending messages quickly. Please wait a moment and try again."
DAILY_LIMIT_MSG = (
    "The app has reached its question limit for today. Please try again tomorrow."
)
INVALID_MSG = (
    "Sorry, I couldn't turn that into a weather query. Could you rephrase it, "
    "with a city and a time period? Type /help for examples."
)
# Shown (with a "Plot it" button) for a chart request without /plot
NEEDS_PLOT_MSG = (
    "That needs a chart. Charts use the /plot command: click **Plot it** to "
    "chart this question, or start a message with /plot."
)
# A /plot turn the resolver couldn't turn into a chart
NOT_CHARTABLE_MSG = (
    "I couldn't turn that into a chart. Charts show values by day (a year or less), month, season, or year, "
    "e.g. `/plot average high and low for each month at Dulles` or "
    "`/plot summer highs at Reagan National with a trend line`."
)
GENERIC_CLARIFICATION_MSG = (
    "Could you rephrase that with a city and a time period? " "Type /help for examples."
)

# Explicit chart words. Outside plot mode these get NEEDS_PLOT_MSG without a
# model call; implicit chart requests ("for each month") are left to the
# resolver's needs_plot clarification. Narrow on purpose: "normal" or
# "trend" alone isn't here, so "the normal July high" still answers.
CHART_WORDS = re.compile(
    r"\b(plot|plots|plotting|plotted|chart|charts|charting|graph|graphs|graphing"
    r"|visuali[sz]e|visuali[sz]ation|trend ?lines?)\b",
    re.IGNORECASE,
)

# Prompt-injection phrasings. Kept narrow to avoid false positives on real
# weather questions; the resolver's off_topic status is the broader net.
INJECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(ignore|disregard|forget|override)\b.{0,40}\b(instructions?|prompts?|rules|directions)\b",
        r"\bsystem\s+prompt\b",
        r"\byou\s+are\s+now\b",
        r"\bdeveloper\s+mode\b",
        r"\bjailbreak",
        r"\b(reveal|print|show|repeat|output)\b.{0,30}\b(your|the)\s+(prompt|instructions|rules)\b",
        r"</?\s*(system|assistant|instructions?)\s*>",
        r"[A-Za-z0-9+/]{80,}={0,2}",  # long base64-looking blob
    )
]


# ---------------------------------------------------------------------------
# Session limits
# ---------------------------------------------------------------------------


@dataclass
class SessionLimit:
    """Why a session can't take more questions: input placeholder + message."""

    reason: str  # "ended" (off-topic streak) | "questions" | "messages"
    placeholder: str
    message: str


def questions_remaining(session) -> int:
    return max(SESSION_QUESTION_LIMIT - session.resolved_count, 0)


def session_limit(session) -> SessionLimit | None:
    """
    The limit this session has hit, or None if it can keep asking. Reads
    only the QuerySession counters (off_topic_streak, resolved_count,
    turn_index); the daily cap needs a database count, so the caller checks
    it separately against DAILY_QUESTION_LIMIT.
    """
    if session.off_topic_streak >= OFF_TOPIC_STREAK_LIMIT:
        return SessionLimit("ended", "Session ended", SESSION_ENDED_MSG)
    if session.resolved_count >= SESSION_QUESTION_LIMIT:
        return SessionLimit(
            "questions",
            "Question limit reached for this session",
            f"You've used all {SESSION_QUESTION_LIMIT} questions for this session. "
            "Thanks for trying the app!",
        )
    if session.turn_index >= SESSION_TURN_LIMIT:
        return SessionLimit(
            "messages",
            "Message limit reached for this session",
            "This session has reached its message limit. Thanks for trying the app!",
        )
    return None


# ---------------------------------------------------------------------------
# Pre-checks (before Claude)
# ---------------------------------------------------------------------------


@dataclass
class Blocked:
    """A message stopped before reaching Claude."""

    reply: str
    reason: str
    off_topic: bool = False  # counts toward the off-topic streak


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def pre_check(
    question: str,
    *,
    history: list[dict],
    recent_message_times: list[float],
    now: float,
) -> Blocked | None:
    """
    Returns Blocked if the question shouldn't go to Claude, else None.
    recent_message_times are earlier attempts' timestamps (same clock as
    `now`); the caller records `now` after this returns.
    """
    if len(question) > MAX_QUESTION_CHARS:
        return Blocked(TOO_LONG_MSG, "too_long")

    for pattern in INJECTION_PATTERNS:
        if pattern.search(question):
            return Blocked(
                OFF_TOPIC_MSG,
                f"injection_pattern:{pattern.pattern[:40]}",
                off_topic=True,
            )

    window_start = now - RATE_LIMIT_WINDOW_SECS
    if sum(1 for t in recent_message_times if t >= window_start) >= RATE_LIMIT_MESSAGES:
        return Blocked(SLOW_DOWN_MSG, "rate_limit")

    # Same question as the previous turn: re-show its answer. Not after an
    # error, so a retry of a failed question still goes through.
    if history:
        last = history[-1]
        if _normalize(question) == _normalize(last["question"]) and not last[
            "answer"
        ].startswith("[error]"):
            return Blocked(last["answer"], "repeat")

    return None


# ---------------------------------------------------------------------------
# Resolver output validation (before the warehouse)
# ---------------------------------------------------------------------------


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_threshold(name: str, threshold, problems: list[str]) -> None:
    if not isinstance(threshold, dict):
        problems.append(f"{name} is not an object")
        return
    if threshold.get("operator") not in OPERATORS:
        problems.append(f"{name}.operator {threshold.get('operator')!r} not allowed")
    if not _is_number(threshold.get("value")):
        problems.append(f"{name}.value {threshold.get('value')!r} is not a number")


def _parse_date(value) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _validate_query(q, station_info: dict, today: date) -> list[str]:
    if not isinstance(q, dict):
        return ["query is not an object"]
    problems: list[str] = []

    # Must be a station lookup_station returned this session (or an earlier
    # turn), not an ID the model made up.
    if q.get("station_id") not in station_info:
        problems.append(
            f"station_id {q.get('station_id')!r} wasn't returned by lookup_station"
        )

    metric = q.get("metric")
    aggregation = q.get("aggregation")
    unit = q.get("unit", "day")
    if metric not in METRICS:
        problems.append(f"metric {metric!r} not allowed")
    if aggregation not in AGGREGATIONS:
        problems.append(f"aggregation {aggregation!r} not allowed")
    if unit not in UNITS:
        problems.append(f"unit {unit!r} not allowed")
    if aggregation in EVENT_ONLY_AGGREGATIONS and unit != "event":
        problems.append(f"aggregation {aggregation!r} requires unit 'event'")

    if unit == "event":
        if metric not in EVENT_METRICS:
            problems.append(f"unit 'event' requires PRCP or SNOW, got {metric!r}")
        if "event_day_threshold" not in q:
            problems.append("unit 'event' requires event_day_threshold")
        if q.get("event_value", "total") not in EVENT_VALUES:
            problems.append(f"event_value {q.get('event_value')!r} not allowed")
    if "limit" in q:
        lim = q["limit"]
        if aggregation not in RANKING_AGGREGATIONS:
            problems.append(f"limit only applies to top/bottom, not {aggregation!r}")
        elif not (
            isinstance(lim, int)
            and not isinstance(lim, bool)
            and 1 <= lim <= MAX_RANK_LIMIT
        ):
            problems.append(f"limit {lim!r} must be an integer 1-{MAX_RANK_LIMIT}")
    if unit == "season" and q.get("season") not in SEASONS:
        problems.append(
            f"unit 'season' requires season winter, spring, summer, or fall, got {q.get('season')!r}"
        )
    if "calendar_day" in q and (unit != "day_of_year" or aggregation == "series"):
        problems.append("calendar_day only applies to a day_of_year record or normal")
    if aggregation in DATE_AGGREGATIONS:
        if unit not in DATE_UNITS:
            problems.append(
                f"aggregation {aggregation!r} needs unit year or snow_season"
            )
        if "threshold" not in q:
            problems.append(f"aggregation {aggregation!r} requires a threshold")
    if aggregation == "series":
        _validate_series(q, unit, today, problems)
    elif unit == "day_of_year":
        if aggregation not in CALENDAR_DAY_AGGREGATIONS:
            problems.append(
                f"a calendar-day query takes max, min, or mean, not {aggregation!r}"
            )
        pa = q.get("period_aggregation")
        if pa is not None and not (
            pa == "any" and aggregation == "mean" and "threshold" in q
        ):
            problems.append(
                "a calendar-day query takes period_aggregation only as any (odds), with mean and a threshold"
            )
        cd = q.get("calendar_day")
        try:
            date.fromisoformat(f"2000-{cd}")  # a leap year, so 02-29 is valid
        except (TypeError, ValueError):
            problems.append(f"calendar_day {cd!r} must be MM-DD")
    elif unit in CLIMATOLOGY_UNITS:
        problems.append(f"unit {unit!r} only applies to series")
    else:
        for name in ("trend", "baseline", "display"):
            if name in q:
                problems.append(f"{name} only applies to series")
    if (
        unit
        in {
            "month",
            "year",
            "snow_season",
            "season",
            "month_of_year",
            "season_of_year",
            "week_of_year",
        }
        and aggregation not in DATE_AGGREGATIONS
    ):
        allowed = (
            SERIES_PERIOD_AGGREGATIONS
            if aggregation == "series"
            else PERIOD_AGGREGATIONS
        )
        if q.get("period_aggregation") not in allowed:
            problems.append(
                f"unit {unit!r} requires period_aggregation {' or '.join(sorted(allowed))}"
            )
    # a count series may use the Jul-Jun year for any metric (freeze days per cold season)
    if (
        unit == "snow_season"
        and metric != "SNOW"
        and not (aggregation == "series" and q.get("period_aggregation") == "count")
    ):
        problems.append(f"unit 'snow_season' requires SNOW, got {metric!r}")

    for name in ("threshold", "event_day_threshold"):
        if name in q:
            _check_threshold(name, q[name], problems)
    if "output_unit" in q and q["output_unit"] not in OUTPUT_UNITS:
        problems.append(f"output_unit {q['output_unit']!r} not allowed")
    if "month_filter" in q:
        mf = q["month_filter"]
        if not (
            isinstance(mf, list)
            and mf
            and all(isinstance(m, int) and 1 <= m <= 12 for m in mf)
        ):
            problems.append(f"month_filter {mf!r} must be a list of months 1-12")
        # query_by_period ignores it for any other unit, so e.g. "hottest
        # summers" as unit year + [6, 7, 8] would silently rank whole years
        # unit day: daily rows in those months across years ("the hottest
        # January day"); not for a daily series, which query_series doesn't filter
        dates = (
            aggregation in DATE_AGGREGATIONS
            or q.get("period_aggregation") in DATE_AGGREGATIONS
        )
        if (
            unit not in ("month", "value")
            and not (unit == "day" and aggregation != "series")
            and not (dates and unit == "year")
        ):
            problems.append(
                f"month_filter only applies to unit 'month' or 'day', not {unit!r}"
            )

    start, end = _parse_date(q.get("start_date")), _parse_date(q.get("end_date"))
    if start is None or end is None:
        problems.append("start_date/end_date must be YYYY-MM-DD")
    else:
        if start > end:
            problems.append("start_date is after end_date")
        if start < MIN_DATE:
            problems.append(f"start_date before {MIN_DATE}")
        # Only a range that hasn't started yet is a forecast. A range already
        # under way ("October 2026", "this year") legitimately ends in the
        # future; the query just finds observations up to today.
        if start > today:
            problems.append("start_date is in the future")
    return problems


def coerce_dates(resolved: dict, question: str = "") -> None:
    """
    "The earliest first fall freeze" sometimes comes back as aggregation
    earliest/latest (the event aggregations) on a yearly query. With a
    threshold on year/snow_season buckets it can only mean a first/last date:
    which one follows the question's "first"/"last", else earliest -> first.
    """
    words = question.lower()
    for q in resolved.get("queries") or []:
        if (
            isinstance(q, dict)
            and q.get("aggregation") in EVENT_ONLY_AGGREGATIONS
            and q.get("unit") in DATE_UNITS
            and "threshold" in q
        ):
            first = (
                "first" in words
                if ("first" in words) != ("last" in words)
                else q["aggregation"] == "earliest"
            )
            q["aggregation"] = "first_date" if first else "last_date"


def cap_rank_limits(resolved: dict) -> None:
    """
    Trims a top/bottom limit over MAX_RANK_LIMIT down to it, in place, and
    keeps what was asked for as requested_limit so the answer can say so
    ("top 20" -> 15 ranks). Runs before validate_resolved, so an over-cap
    limit is answered instead of rejected; anything else odd about limit
    (not an integer, below 1) is still left for validation to reject.
    A limit of 1 on max/min ("which year was wettest" resolved as max with
    limit 1) is dropped: max/min already is the single top value.
    """
    for q in resolved.get("queries") or []:
        if not isinstance(q, dict):
            continue
        if q.get("aggregation") in {"max", "min"} and q.get("limit") == 1:
            del q["limit"]
        if q.get("aggregation") not in RANKING_AGGREGATIONS:
            continue
        lim = q.get("limit")
        if isinstance(lim, int) and not isinstance(lim, bool) and lim > MAX_RANK_LIMIT:
            q["requested_limit"] = lim
            q["limit"] = MAX_RANK_LIMIT


def _validate_series(q: dict, unit: str, today: date, problems: list[str]) -> None:
    """Fields only a series (a /plot query, references/plotting.md) may carry."""
    if unit not in SERIES_UNITS:
        problems.append(f"series unit {unit!r} not allowed")
    if unit == "day":
        start, end = _parse_date(q.get("start_date")), _parse_date(q.get("end_date"))
        if start and end and (end - start).days > MAX_DAILY_SERIES_DAYS:
            problems.append(f"daily series span more than {MAX_DAILY_SERIES_DAYS} days")
        # a trend within a year isn't one
        if "trend" in q:
            problems.append("trend doesn't apply to a daily series")
        if "period_aggregation" in q:
            problems.append("a daily series has no period_aggregation")
    elif unit == "value":
        for name in ("period_aggregation", "baseline", "display", "trend", "threshold"):
            if name in q:
                problems.append(f"{name} doesn't apply to a histogram of daily values")
    elif (
        q.get("period_aggregation") in {"count", "any"} | DATE_AGGREGATIONS
        and "threshold" not in q
    ):
        problems.append(
            f"period_aggregation {q['period_aggregation']} requires a threshold"
        )
    if (
        "threshold" in q
        and q.get("period_aggregation") not in {"count", "any"} | DATE_AGGREGATIONS
    ):
        problems.append(
            "a series threshold requires period_aggregation count, any, first_date, or last_date"
        )
    if q.get("period_aggregation") in DATE_AGGREGATIONS and unit not in DATE_UNITS:
        problems.append(f"{q['period_aggregation']} needs unit year or snow_season")
    if q.get("period_aggregation") == "any" and unit not in CLIMATOLOGY_UNITS:
        problems.append("period_aggregation any (odds) needs a climatology unit")
    if "trend" in q:
        if q["trend"] is not True:
            problems.append(f"trend {q['trend']!r} must be true or omitted")
        elif unit in CLIMATOLOGY_UNITS:
            problems.append("trend doesn't apply to a climatology")
    if "baseline" in q:
        b = q["baseline"]
        start, end = (
            (b.get("start_year"), b.get("end_year"))
            if isinstance(b, dict)
            else (None, None)
        )
        if (
            not (isinstance(start, int) and isinstance(end, int))
            or isinstance(start, bool)
            or isinstance(end, bool)
        ):
            problems.append(f"baseline {b!r} needs integer start_year and end_year")
        else:
            years = end - start + 1
            if not MIN_BASELINE_YEARS <= years <= MAX_BASELINE_YEARS:
                problems.append(
                    f"baseline is {years} years ({MIN_BASELINE_YEARS}-{MAX_BASELINE_YEARS} allowed)"
                )
            if end >= today.year:
                problems.append(f"baseline must end before {today.year}")
        if unit in CLIMATOLOGY_UNITS:
            problems.append(
                "baseline doesn't apply to a climatology (its dates are the period)"
            )
    if "display" in q:
        if q["display"] not in DISPLAYS:
            problems.append(f"display {q['display']!r} not allowed")
        elif q["display"] == "growing_season":
            if (
                q.get("period_aggregation") not in DATE_AGGREGATIONS
                or q.get("metric") != "TMIN"
            ):
                problems.append(
                    "display 'growing_season' pairs TMIN last_date (spring) and first_date (fall) series"
                )
        elif q["display"] == "difference":
            if "baseline" in q or unit == "value":
                problems.append(
                    "display 'difference' compares two series as they are: no baseline or histogram"
                )
        elif q["display"] == "climograph":
            if unit != "month_of_year" or q.get("metric") not in CLIMOGRAPH_METRICS:
                problems.append(
                    "display 'climograph' is monthly climatologies (month_of_year) of temperature and PRCP/SNOW"
                )
        elif q["display"] == "histogram":
            if unit not in HISTOGRAM_UNITS:
                problems.append(
                    f"display 'histogram' needs unit month, year, season, or snow_season, not {unit!r}"
                )
            for name in ("baseline", "trend"):
                if name in q:
                    problems.append(f"{name} doesn't apply to a histogram")
        elif q["display"] in DAILY_DISPLAYS:
            if unit != "day":
                problems.append(f"display {q['display']!r} needs unit day")
            if q.get("metric") not in DAILY_DISPLAYS[q["display"]]:
                problems.append(
                    f"display {q['display']!r} doesn't apply to {q.get('metric')!r}"
                )
            if q["display"] == "climate" and "baseline" not in q:
                problems.append("display 'climate' needs a baseline")
        elif "baseline" not in q:
            problems.append("display needs a baseline")


DAILY_LIMIT_NOTE = (
    "Daily points are limited to a year or less, so this shows monthly values."
)
DAILY_WORDS = re.compile(
    r"\b(daily|each day|every day|per day|day[- ]by[- ]day)\b", re.IGNORECASE
)


def coerce_series(resolved: dict, question: str = "") -> None:
    """
    Fixes series queries that ask for more than plots allow, in place, before
    validation -- a chart instead of an error, like cap_rank_limits. Daily
    points over more than MAX_DAILY_SERIES_DAYS become monthly; a monthly
    series over MAX_MONTHLY_SERIES_YEARS becomes yearly. Each change leaves a
    note under the chart (notes). `question` is the user's text: a long-span
    request for daily values gets the note even when the resolver already
    picked monthly itself (plotting.md says to).
    """
    for q in resolved.get("queries") or []:
        if not isinstance(q, dict) or q.get("aggregation") != "series":
            continue
        start, end = _parse_date(q.get("start_date")), _parse_date(q.get("end_date"))
        long_span = bool(start and end and (end - start).days > MAX_DAILY_SERIES_DAYS)
        if q.get("unit", "day") == "day" and long_span:
            q["unit"] = "month"
            # monthly buckets need one; a daily mean is the closest reading
            q.setdefault(
                "period_aggregation",
                "sum" if q.get("metric") in {"PRCP", "SNOW"} else "mean",
            )
            q.setdefault("notes", []).append(DAILY_LIMIT_NOTE)
        elif (
            q.get("unit", "day") == "day"
            and q.get("period_aggregation") in PERIOD_AGGREGATIONS
        ):
            # a day is its own value; mean/sum of one day is a no-op
            del q["period_aggregation"]
        elif q.get("unit") == "month" and long_span and DAILY_WORDS.search(question):
            q.setdefault("notes", []).append(DAILY_LIMIT_NOTE)
        elif (
            q.get("unit") == "month"
            and start
            and end
            and not long_span
            and DAILY_WORDS.search(question)
        ):
            # asked for daily values over a year or less, resolved as monthly anyway
            q["unit"] = "day"
            q.pop("period_aggregation", None)
            q.pop("trend", None)
        if (
            q.get("unit") == "month"
            and start
            and end
            and (end.year - start.year + 1) > MAX_MONTHLY_SERIES_YEARS
        ):
            q["unit"] = "year"
            q.setdefault("notes", []).append(
                f"Monthly points are limited to {MAX_MONTHLY_SERIES_YEARS} years, so this shows yearly values."
            )


def _cross_query_displays(queries: list[dict]) -> list[str]:
    """Displays that need a set of queries: a difference pairs two, a climograph needs both kinds of line."""
    problems = []
    diff = [q for q in queries if q.get("display") == "difference"]
    if diff and (
        len(diff) != 2
        or len({q.get("metric") for q in diff}) != 1
        or len({q.get("unit") for q in diff}) != 1
        or len({q.get("station_id") for q in diff}) != 2
    ):
        problems.append(
            "display 'difference' needs exactly two queries: same metric and unit, two stations"
        )
    grow = [q for q in queries if q.get("display") == "growing_season"]
    if grow and sorted(q.get("period_aggregation") for q in grow) != [
        "first_date",
        "last_date",
    ]:
        problems.append(
            "display 'growing_season' needs a last_date (spring) and a first_date (fall) query"
        )
    clim = [q for q in queries if q.get("display") == "climograph"]
    if clim and not (
        {q.get("metric") for q in clim} & {"TMAX", "TMIN", "TAVG"}
        and {q.get("metric") for q in clim} & {"PRCP", "SNOW"}
    ):
        problems.append(
            "display 'climograph' needs a temperature query and a PRCP/SNOW query"
        )
    return problems


def validate_resolved(
    resolved: dict,
    station_info: dict,
    today: date | None = None,
    *,
    plot_mode: str | None = PLOT_OFF,
) -> list[str]:
    """
    Problems with the resolver's output, or [] if it's safe to act on.
    Clarifications are checked too, since their text is shown to the user.
    plot_mode: PLOT_EXPLICIT (/plot: every query must be a series),
    PLOT_INHERITED (a follow-up to a chart: either kind), or PLOT_OFF (no
    series).
    """
    today = today or date.today()
    status = resolved.get("status")
    if status not in STATUSES:
        return [f"status {status!r} not allowed"]

    if status == "resolved":
        queries = resolved.get("queries")
        if not isinstance(queries, list) or not queries:
            return ["resolved output has no queries"]
        if len(queries) > MAX_QUERIES_PER_QUESTION:
            return [f"{len(queries)} queries (max {MAX_QUERIES_PER_QUESTION})"]
        problems = [p for q in queries for p in _validate_query(q, station_info, today)]
        series = [
            isinstance(q, dict) and q.get("aggregation") == "series" for q in queries
        ]
        rankings = [
            isinstance(q, dict) and q.get("aggregation") in RANKING_AGGREGATIONS
            for q in queries
        ]
        if plot_mode == PLOT_OFF and any(series):
            problems.append("series outside plot mode")
        # a /plot of a ranking ("the 10 biggest snowstorms") draws its rows as bars
        elif plot_mode == PLOT_EXPLICIT and not (all(series) or all(rankings)):
            problems.append("/plot answered without a series")
        elif any(series) and not all(series):
            problems.append("series mixed with single-value queries")
        problems += _cross_query_displays([q for q in queries if isinstance(q, dict)])
        return problems

    if "reason" in resolved and resolved["reason"] not in CLARIFICATION_REASONS:
        return [f"reason {resolved['reason']!r} not allowed"]

    if status == "needs_clarification":
        text = resolved.get("clarification_question")
        if not isinstance(text, str) or not text.strip():
            return ["empty clarification"]
        if len(text) > MAX_CLARIFICATION_CHARS:
            return [
                f"clarification is {len(text)} chars (max {MAX_CLARIFICATION_CHARS})"
            ]
        # Prose the model wrote instead of JSON (see pipeline.resolve_question)
        # is only shown if it reads as a question.
        if resolved.get("from_prose") and not text.rstrip().endswith("?"):
            return ["non-JSON reply that isn't a question"]
    return []
