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
SESSION_QUESTION_LIMIT = int(os.environ.get("WX_SESSION_QUESTION_LIMIT", "10"))
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

# ---------------------------------------------------------------------------
# Allowed values (SKILL.md Step 6)
# ---------------------------------------------------------------------------

METRICS = {"TAVG", "TMAX", "TMIN", "PRCP", "SNOW", "SNWD", "AWND", "HDD", "CDD"}
AGGREGATIONS = {"mean", "median", "sum", "max", "min", "stddev", "count", "latest", "earliest"}
UNITS = {"day", "event", "month", "year"}
OPERATORS = {">", ">=", "<", "<=", "="}
EVENT_METRICS = {"PRCP", "SNOW"}
EVENT_ONLY_AGGREGATIONS = {"latest", "earliest"}
EVENT_VALUES = {"total", "duration"}
PERIOD_AGGREGATIONS = {"mean", "sum"}
OUTPUT_UNITS = {"C", "F"}
STATUSES = {"resolved", "needs_clarification", "off_topic"}
MIN_DATE = date(1800, 1, 1)

# ---------------------------------------------------------------------------
# Canned replies (shown instead of model-written text)
# ---------------------------------------------------------------------------

OFF_TOPIC_MSG = (
    "I can only answer questions about past weather at the stations in the "
    "Home location list. Type /help for examples."
)
SESSION_ENDED_MSG = (
    "This session has ended after several off-topic messages. "
    "Thanks for trying the app!"
)
TOO_LONG_MSG = (
    f"Please keep questions under {MAX_QUESTION_CHARS} characters."
)
SLOW_DOWN_MSG = "You're sending messages quickly. Please wait a moment and try again."
DAILY_LIMIT_MSG = "The app has reached its question limit for today. Please try again tomorrow."
INVALID_MSG = (
    "Sorry, I couldn't turn that into a weather query. Could you rephrase it, "
    "with a city and a time period? Type /help for examples."
)
GENERIC_CLARIFICATION_MSG = (
    "Could you rephrase that with a city and a time period? "
    "Type /help for examples."
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
            return Blocked(OFF_TOPIC_MSG, f"injection_pattern:{pattern.pattern[:40]}", off_topic=True)

    window_start = now - RATE_LIMIT_WINDOW_SECS
    if sum(1 for t in recent_message_times if t >= window_start) >= RATE_LIMIT_MESSAGES:
        return Blocked(SLOW_DOWN_MSG, "rate_limit")

    # Same question as the previous turn: re-show its answer. Not after an
    # error, so a retry of a failed question still goes through.
    if history:
        last = history[-1]
        if _normalize(question) == _normalize(last["question"]) and not last["answer"].startswith("[error]"):
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
        problems.append(f"station_id {q.get('station_id')!r} wasn't returned by lookup_station")

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
    if unit in ("month", "year") and q.get("period_aggregation") not in PERIOD_AGGREGATIONS:
        problems.append(f"unit {unit!r} requires period_aggregation mean or sum")

    for name in ("threshold", "event_day_threshold"):
        if name in q:
            _check_threshold(name, q[name], problems)
    if "output_unit" in q and q["output_unit"] not in OUTPUT_UNITS:
        problems.append(f"output_unit {q['output_unit']!r} not allowed")
    if "month_filter" in q:
        mf = q["month_filter"]
        if not (isinstance(mf, list) and mf and all(isinstance(m, int) and 1 <= m <= 12 for m in mf)):
            problems.append(f"month_filter {mf!r} must be a list of months 1-12")

    start, end = _parse_date(q.get("start_date")), _parse_date(q.get("end_date"))
    if start is None or end is None:
        problems.append("start_date/end_date must be YYYY-MM-DD")
    else:
        if start > end:
            problems.append("start_date is after end_date")
        if start < MIN_DATE:
            problems.append(f"start_date before {MIN_DATE}")
        if end > today:
            problems.append("end_date is in the future")
    return problems


def validate_resolved(resolved: dict, station_info: dict, today: date | None = None) -> list[str]:
    """
    Problems with the resolver's output, or [] if it's safe to act on.
    Clarifications are checked too, since their text is shown to the user.
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
        return [p for q in queries for p in _validate_query(q, station_info, today)]

    if status == "needs_clarification":
        text = resolved.get("clarification_question")
        if not isinstance(text, str) or not text.strip():
            return ["empty clarification"]
        if len(text) > MAX_CLARIFICATION_CHARS:
            return [f"clarification is {len(text)} chars (max {MAX_CLARIFICATION_CHARS})"]
        # Prose the model wrote instead of JSON (see pipeline.resolve_question)
        # is only shown if it reads as a question.
        if resolved.get("from_prose") and not text.rstrip().endswith("?"):
            return ["non-JSON reply that isn't a question"]
    return []
