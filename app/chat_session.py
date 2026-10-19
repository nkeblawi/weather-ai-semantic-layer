"""
Session management for the weather-query-resolver skill: wraps the shared
pipeline in app/pipeline.py with per-session state and chat_store.py
persistence, for callers with no CLI of their own (e.g. a future Streamlit
chat UI).
"""

from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# app/ is not a package (no __init__.py). Put app/ itself on sys.path before
# importing pipeline/chat_store, so these bare imports resolve regardless of
# the caller's cwd or how THIS module was imported (e.g. from a Streamlit
# entry point elsewhere in the repo). Must run before the imports below.
_APP_DIR = str(Path(__file__).resolve().parent)
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

import pipeline  # noqa: E402
import chat_store  # noqa: E402
import guardrails  # noqa: E402
import series  # noqa: E402

logger = logging.getLogger(__name__)

PLOT_COMMAND = "/plot"
PLOT_USAGE_MSG = (
    "Add a question after /plot, e.g. `/plot average high and low for each month at Dulles`."
)


def plot_mode_for(session: "QuerySession", question: str) -> tuple[str | None, str]:
    """
    (plot mode, question text without /plot). /plot -> an explicit plot
    turn; otherwise a follow-up to a chart (the last turn the model saw was a
    plot turn) inherits plot mode; anything else is a regular turn.
    """
    text = question.strip()
    if text.lower() == PLOT_COMMAND or text.lower().startswith(PLOT_COMMAND + " "):
        return guardrails.PLOT_EXPLICIT, text[len(PLOT_COMMAND):].strip()
    last = next((t for t in reversed(session.history) if not t.get("ui_only")), None)
    if last and last.get("plot_turn"):
        return guardrails.PLOT_INHERITED, text
    return guardrails.PLOT_OFF, text


@dataclass
class QuerySession:
    """
    Session-scoped state for one chat session, matching the local variables
    app/test_harness.py's main() holds across its while-loop. Create via
    create_session(); pass the same instance to get_answer() once per
    question.

    conn/client are supplied by the caller and are not owned or
    closed here (construct via pipeline.get_databricks_connection() and
    anthropic.Anthropic(), e.g. behind st.cache_resource).
    """

    conn: Any
    client: Any
    user_id: str
    session_id: str
    temp_unit: str
    home_location: str | None
    turn_index: int = 0
    # Turns that resolved and ran answer_query -- what usage limits count.
    # Clarifications and errors don't.
    resolved_count: int = 0
    # Off-topic messages in a row (keyword-blocked or resolver off_topic);
    # reset by a resolved answer. guardrails.OFF_TOPIC_STREAK_LIMIT ends the
    # session -- see guardrails.session_limit().
    off_topic_streak: int = 0
    # time.monotonic() of each attempt, for the rate limit
    recent_message_times: list[float] = field(default_factory=list)
    # Turns shown in the chat. Entries with "ui_only" (blocked, off-topic,
    # invalid) are display-only and never replayed to the model.
    history: list[dict] = field(default_factory=list)
    station_info: dict = field(default_factory=dict)


def create_session(
    conn,
    client,
    username: str,
    *,
    email: str | None = None,
    display_name: str | None = None,
    temp_unit: str | None = None,
    home_location: str | None = None,
) -> QuerySession:
    """
    Opens one chat session for `username`.
    """
    user_id, resolved_temp_unit, resolved_home_location = chat_store.ensure_user(
        conn,
        username,
        email=email,
        display_name=display_name,
        temp_unit=temp_unit,
        home_location=home_location,
    )
    return QuerySession(
        conn=conn,
        client=client,
        user_id=user_id,
        session_id=chat_store.new_session_id(),
        temp_unit=resolved_temp_unit,
        home_location=resolved_home_location,
    )


def update_preferences(
    session: QuerySession, *, temp_unit: str, home_location: str | None
) -> None:
    """
    Persists the user's temp_unit / home_location and applies them to
    `session` so the next get_answer() uses them. Raises on write failure;
    the session is only changed once the write succeeds.
    """
    chat_store.update_user_prefs(
        session.conn, session.user_id, temp_unit, home_location
    )
    session.temp_unit = temp_unit
    session.home_location = home_location


def get_answer(
    session: QuerySession,
    question: str,
    *,
    over_daily_limit: Callable[[], bool] | None = None,
) -> str:
    """
    Answers one question within `session`. Every step below
    calls into pipeline's and chat_store's existing public functions only.

    Mutates session.history and session.station_info in place by
    pipeline.resolve_question itself on each lookup_station hit, and
    session.turn_index. Returns the answer text.

    Matches the CLI's error-handling convention exactly: on any internal
    failure (Claude API error, SQL error, etc.) this returns
    "[error] {exc}" as the answer text rather than raising.

    This does not raise for ordinary runtime failures. To distinguish between
    resolved / needs_clarification / error, function calls should check the
    returned string's "[error] " prefix for now (status is still recorded via
    chat_store.record_turn, just not returned).

    Guardrails (app/guardrails.py): a message blocked by pre_check never
    reaches Claude and isn't recorded; resolver output that fails
    validate_resolved is answered with a canned reply (status "invalid") and
    nothing runs on the warehouse. off_topic replies are canned too -- the
    model's own text is never shown for them.

    over_daily_limit (optional) is checked only after pre_check passes, so a
    blocked message never costs the warehouse query it may run.
    """
    now = time.monotonic()
    blocked = guardrails.pre_check(
        question,
        history=session.history,
        recent_message_times=session.recent_message_times,
        now=now,
    )
    session.recent_message_times = [
        t for t in session.recent_message_times
        if t >= now - guardrails.RATE_LIMIT_WINDOW_SECS
    ] + [now]
    if blocked:
        logger.info("blocked (%s) session=%s", blocked.reason, session.session_id)
        if blocked.off_topic:
            session.off_topic_streak += 1
        session.history.append({"question": question, "answer": blocked.reply, "query": None, "ui_only": True})
        return blocked.reply
    plot_mode, text = plot_mode_for(session, question)
    if plot_mode == guardrails.PLOT_EXPLICIT and not text:
        session.history.append({"question": question, "answer": PLOT_USAGE_MSG, "query": None, "ui_only": True})
        return PLOT_USAGE_MSG
    # Explicit chart words without /plot: offer the Plot it button, no model call
    if plot_mode == guardrails.PLOT_OFF and guardrails.CHART_WORDS.search(text):
        session.history.append(
            {"question": question, "answer": guardrails.NEEDS_PLOT_MSG, "query": None, "ui_only": True, "needs_plot": True}
        )
        return guardrails.NEEDS_PLOT_MSG
    if over_daily_limit and over_daily_limit():
        session.history.append(
            {"question": question, "answer": guardrails.DAILY_LIMIT_MSG, "query": None, "ui_only": True}
        )
        return guardrails.DAILY_LIMIT_MSG

    resolved: dict = {}
    answer_text: str | None = None
    tables: list[dict] = []
    charts: list[dict] = []
    needs_plot = False
    status = "error"
    # Display-only turns are never replayed to the model as context
    model_history = [t for t in session.history if not t.get("ui_only")]

    try:
        resolved = pipeline.resolve_question(
            session.client,
            session.conn,
            text,
            testing=False,
            history=model_history,
            station_info=session.station_info,
            home_location=session.home_location,
            plot_mode=plot_mode,
        )

        try:
            pipeline.fill_station_info(session.conn, resolved, session.station_info)
        except Exception as exc:  # noqa: BLE001 -- unchecked ids are rejected below
            logger.warning("station id check failed session=%s: %s", session.session_id, exc)
        guardrails.cap_rank_limits(resolved)
        guardrails.coerce_dates(resolved, text)
        guardrails.coerce_series(resolved, text)
        problems = guardrails.validate_resolved(resolved, session.station_info, plot_mode=plot_mode)
        if problems:
            logger.warning("invalid resolver output session=%s: %s", session.session_id, problems)
            if plot_mode == guardrails.PLOT_EXPLICIT and problems == ["/plot answered without a series"]:
                # a single value for a /plot request: say what can be charted
                answer_text = guardrails.NOT_CHARTABLE_MSG
                status = "needs_clarification"
            elif resolved.get("status") == "needs_clarification" and plot_mode == guardrails.PLOT_EXPLICIT:
                answer_text = guardrails.NOT_CHARTABLE_MSG
                status = "needs_clarification"
            elif resolved.get("status") == "needs_clarification":
                # A bad clarification (too long, prose that isn't a question)
                # still means "ask the user"; just don't show the model's text.
                answer_text = guardrails.GENERIC_CLARIFICATION_MSG
                status = "needs_clarification"
            else:
                answer_text = guardrails.INVALID_MSG
                status = "invalid"
        elif resolved.get("status") == "off_topic":
            answer_text = guardrails.OFF_TOPIC_MSG
            status = "off_topic"
        elif resolved.get("reason") == "needs_plot" and plot_mode == guardrails.PLOT_EXPLICIT:
            # told it was a /plot turn but asked for /plot anyway
            answer_text = guardrails.NOT_CHARTABLE_MSG
            status = "needs_clarification"
        elif resolved.get("reason") == "needs_plot" and plot_mode == guardrails.PLOT_OFF:
            # a chart request without /plot (e.g. "for each month"): canned
            # message plus the Plot it button
            answer_text = guardrails.NEEDS_PLOT_MSG
            status = "needs_clarification"
            needs_plot = True
        elif resolved.get("status") != "resolved":
            answer_text = resolved.get(
                "clarification_question", "I need more information to answer that."
            )
            status = resolved.get("status", "needs_clarification")
        elif resolved["queries"][0].get("aggregation") == "series":
            # /plot: every query is a series (validate_resolved); a query with
            # a baseline also fetches its normals
            series_list = []
            for entry in resolved["queries"]:
                result = pipeline.call_answer_query(session.conn, entry)
                base_entry = series.baseline_entry(entry)
                base = pipeline.call_answer_query(session.conn, base_entry) if base_entry else None
                rec_entry = series.records_entry(entry)
                records = pipeline.call_answer_query(session.conn, rec_entry) if rec_entry else None
                series_list.append(
                    series.build_series(entry, result, base, session.station_info, session.temp_unit, records)
                )
            charts = series.group_charts(series_list)
            answer_text = pipeline.generate_answer(
                session.client, text, [series.phrasing_summary(x) for x in series_list], history=model_history
            )
            status = "resolved"
        else:
            answer_payloads = []
            for entry in resolved["queries"]:
                result = pipeline.call_answer_query(session.conn, entry)
                if days := pipeline.matching_days(session.conn, entry, result):
                    result = {**result, "matching_days": days}
                answer_payloads.append(
                    pipeline.build_answer_payload(
                        entry, result, session.station_info, session.temp_unit
                    )
                )
            answer_text = pipeline.generate_answer(
                session.client, question, answer_payloads, history=model_history
            )
            tables = [t for p in answer_payloads if (t := pipeline.ranking_table(p))]
            if plot_mode == guardrails.PLOT_EXPLICIT and tables:
                # /plot of a ranking: bars, with the table still under them
                charts = [pipeline.ranking_chart(p, t) for p in answer_payloads if (t := pipeline.ranking_table(p))]
            status = "resolved"
    except Exception as exc:  # noqa: BLE001 -- parity with the CLI's main()
        answer_text = f"[error] {exc}"
        status = "error"

    turn_station_id = None
    if status == "resolved" and resolved.get("queries"):
        turn_station_id = resolved["queries"][0].get("station_id")

    chat_store.record_turn(
        session.conn,
        user_id=session.user_id,
        session_id=session.session_id,
        turn_index=session.turn_index,
        prompt=question,
        response=answer_text,
        status=status,
        resolved_json=json.dumps(resolved) if resolved else None,
        model=pipeline.CLAUDE_MODEL,
        error=answer_text if status == "error" else None,
        station_id=turn_station_id,
    )
    session.turn_index += 1
    if status == "resolved":
        session.resolved_count += 1
        session.off_topic_streak = 0
    elif status == "off_topic":
        session.off_topic_streak += 1

    session.history.append(
        {
            "question": question,
            "answer": answer_text,
            "query": resolved["queries"][0] if status == "resolved" and resolved.get("queries") else None,
            "ui_only": status in ("off_topic", "invalid"),
            # top/bottom results, shown under the answer; display only (not
            # replayed to the model, not saved in chat_history)
            "tables": tables,
            "charts": charts,
            "needs_plot": needs_plot,
            # keeps the next turn in plot mode: a chart, or a /plot turn that
            # asked a clarifying question (its answer is still a plot request)
            "plot_turn": plot_mode is not guardrails.PLOT_OFF and (
                bool(charts) or status == "needs_clarification"
            ),
        }
    )

    return answer_text
