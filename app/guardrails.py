"""
Guardrails for the public, no-sign-in chat: the usage limits and when a
session has hit one (session_limit), cheap checks that run before a question
reaches Claude (pre_check), limits across sessions per client IP and
app-wide (RateLimits), and deterministic validation of what the resolver
returns before anything runs on the warehouse (validate_resolved).

Nothing here calls a model or the database. Blocked messages are never sent
to Claude and never written to chat_history (a write is warehouse compute
too); callers log them instead.

The query checks themselves (allowed values, limits, validate_queries) live
in wxcore.validation, shared with the MCP server; callers import those from
wxcore directly. What stays here is the app's own: the session limits,
canned replies, pre-checks, the checks on the rest of the resolver's reply
(status, clarification text), and the fixes that read the question's
wording.
"""

from __future__ import annotations

import os
import re
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import date

from wxcore.validation import (
    DAILY_LIMIT_NOTE,
    DATE_UNITS,
    EVENT_ONLY_AGGREGATIONS,
    PLOT_OFF,
    series_span,
    validate_queries,
)
from wxcore.validation import cap_rank_limits as _cap_rank_limits
from wxcore.validation import coerce_series as _coerce_series_params


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# --- Limits ---
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

# Usage caps across sessions (RateLimits).
IP_SESSION_LIMIT = int(os.environ.get("WX_IP_SESSION_LIMIT", "10"))
IP_QUESTION_LIMIT = int(os.environ.get("WX_IP_QUESTION_LIMIT", "40"))
IP_LIMIT_WINDOW_SECS = 3600
GLOBAL_SESSION_LIMIT = int(os.environ.get("WX_GLOBAL_SESSION_LIMIT", "30"))
GLOBAL_SESSION_WINDOW_SECS = 60


# --- Allowed values of the resolver's reply itself (SKILL.md Step 6) ---
CLARIFICATION_REASONS = {"needs_plot"}
STATUSES = {"resolved", "needs_clarification", "off_topic"}


# --- Canned replies (shown instead of model-written text) ---
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
IP_SESSION_LIMIT_MSG = (
    "Too many chats have been started from your network recently. "
    "Please try again later."
)
IP_QUESTION_LIMIT_MSG = (
    "Too many questions have come from your network in the last hour. "
    "Please try again later."
)
BUSY_MSG = "The app is busy right now. Please try again in a minute."
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

# Generic clarification message
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


# --- Resolver output validation (before the warehouse) ---
DAILY_WORDS = re.compile(
    r"\b(daily|each day|every day|per day|day[- ]by[- ]day)\b", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

# --- Pre-checks (before Claude) ---


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------

# --- Session limits ---


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


# --- Pre-checks (before Claude) ---


@dataclass
class Blocked:
    """A message stopped before reaching Claude."""

    reply: str
    reason: str
    off_topic: bool = False  # counts toward the off-topic streak


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


# --- Limits across sessions (per IP, app-wide) ---


class SlidingWindow:
    """At most `limit` hits per key in any `window_secs`. Thread-safe."""

    MAX_KEYS = 10_000

    def __init__(self, limit: int, window_secs: float):
        self.limit = limit
        self.window_secs = window_secs
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float) -> bool:
        """Records a hit and returns True, or False if `key` is at the limit."""
        start = now - self.window_secs
        with self._lock:
            if len(self._hits) > self.MAX_KEYS:
                self._hits = {k: h for k, h in self._hits.items() if h and h[-1] > start}
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= start:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True


@dataclass
class RateLimits:
    """Usage caps across sessions; one per process."""

    ip_sessions: SlidingWindow = field(
        default_factory=lambda: SlidingWindow(IP_SESSION_LIMIT, IP_LIMIT_WINDOW_SECS)
    )
    ip_questions: SlidingWindow = field(
        default_factory=lambda: SlidingWindow(IP_QUESTION_LIMIT, IP_LIMIT_WINDOW_SECS)
    )
    global_sessions: SlidingWindow = field(
        default_factory=lambda: SlidingWindow(GLOBAL_SESSION_LIMIT, GLOBAL_SESSION_WINDOW_SECS)
    )

    def new_session(self, ip: str, now: float) -> Blocked | None:
        """Blocked if a new session is over a limit, else None."""
        if not self.ip_sessions.allow(ip, now):
            return Blocked(IP_SESSION_LIMIT_MSG, "ip_session_limit")
        if not self.global_sessions.allow("all", now):
            return Blocked(BUSY_MSG, "global_session_limit")
        return None

    def question(self, ip: str, now: float) -> Blocked | None:
        """Blocked if a question is over a limit, else None."""
        if not self.ip_questions.allow(ip, now):
            return Blocked(IP_QUESTION_LIMIT_MSG, "ip_question_limit")
        return None


# --- Resolver output validation (before the warehouse) ---


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
    """wxcore.validation.cap_rank_limits on the resolver's queries."""
    _cap_rank_limits(resolved.get("queries"))


def coerce_series(resolved: dict, question: str = "") -> None:
    """
    Fixes series queries that ask for more than plots allow, in place, before
    validation -- a chart instead of an error, like cap_rank_limits. Daily
    points over more than MAX_DAILY_SERIES_DAYS become monthly; a monthly
    series over MAX_MONTHLY_SERIES_YEARS becomes yearly. Each change leaves a
    note under the chart (notes). `question` is the user's text: a long-span
    request for daily values gets the note even when the resolver already
    picked monthly itself (plotting.md says to).

    The question-text fixes run here, then the parameter-only ones in
    wxcore.validation.coerce_series. They touch different queries (a monthly
    one here, a daily one there), so the order only matters for a monthly
    query's notes: this one's daily note comes before the yearly one.
    """
    for q in resolved.get("queries") or []:
        if not isinstance(q, dict) or q.get("aggregation") != "series":
            continue
        start, end, long_span = series_span(q)
        if q.get("unit") == "month" and long_span and DAILY_WORDS.search(question):
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
    _coerce_series_params(resolved.get("queries"))


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
    series). The queries go to wxcore.validation.validate_queries.
    """
    status = resolved.get("status")
    if status not in STATUSES:
        return [f"status {status!r} not allowed"]

    if status == "resolved":
        return validate_queries(
            resolved.get("queries"), station_info, today, plot_mode=plot_mode
        )

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
