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

logger = logging.getLogger(__name__)


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
    if over_daily_limit and over_daily_limit():
        session.history.append(
            {"question": question, "answer": guardrails.DAILY_LIMIT_MSG, "query": None, "ui_only": True}
        )
        return guardrails.DAILY_LIMIT_MSG

    resolved: dict = {}
    answer_text: str | None = None
    status = "error"
    # Display-only turns are never replayed to the model as context
    model_history = [t for t in session.history if not t.get("ui_only")]

    try:
        resolved = pipeline.resolve_question(
            session.client,
            session.conn,
            question,
            testing=False,
            history=model_history,
            station_info=session.station_info,
            home_location=session.home_location,
        )

        problems = guardrails.validate_resolved(resolved, session.station_info)
        if problems:
            logger.warning("invalid resolver output session=%s: %s", session.session_id, problems)
            if resolved.get("status") == "needs_clarification":
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
        elif resolved.get("status") != "resolved":
            answer_text = resolved.get(
                "clarification_question", "I need more information to answer that."
            )
            status = resolved.get("status", "needs_clarification")
        else:
            answer_payloads = []
            for entry in resolved["queries"]:
                result = pipeline.call_answer_query(session.conn, entry)
                answer_payloads.append(
                    pipeline.build_answer_payload(
                        entry, result, session.station_info, session.temp_unit
                    )
                )
            answer_text = pipeline.generate_answer(
                session.client, question, answer_payloads, history=model_history
            )
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
        }
    )

    return answer_text
