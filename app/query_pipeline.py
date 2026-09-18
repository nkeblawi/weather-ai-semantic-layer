"""
Streamlit wrapper around the resolve/answer pipeline without using CLI.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# app/ is not a package (no __init__.py). Mirror test_harness.py's own
# sys.path.insert(SCRIPTS_PATH) pattern: put app/ itself on sys.path before
# importing test_harness/chat_store, so `import test_harness` and
# test_harness's own bare `from chat_store import (...)` both resolve
# regardless of the caller's cwd or how THIS module was imported (e.g. from
# a Streamlit entry point elsewhere in the repo). Must run before the
# imports below.
_APP_DIR = str(Path(__file__).resolve().parent)
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

import test_harness  # noqa: E402
import chat_store  # noqa: E402


@dataclass
class QuerySession:
    """
    Session-scoped state for one chat session, matching the local variables
    test_harness.main() holds across its while-loop. Create via
    create_session(); pass the same instance to get_answer() once per
    question. conn/client are supplied by the caller and are NOT owned or
    closed here (construct via test_harness.get_databricks_connection() and
    anthropic.Anthropic(), e.g. behind st.cache_resource).
    """

    conn: Any
    client: Any
    user_id: str
    session_id: str
    temp_unit: str
    home_location: str | None
    turn_index: int = 0
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
    Opens one chat session for `username`. Thin wrapper over
    chat_store.ensure_user (a real MERGE + SELECT -- call this once per
    session, not per question/rerun) + chat_store.new_session_id (pure,
    cheap), matching test_harness.main()'s setup block exactly. Raises on
    ensure_user's write failure, same as the CLI (a startup problem should
    stop the app, not be swallowed).
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


def get_answer(session: QuerySession, question: str) -> str:
    """
    Answers one question within `session`. Reproduces the per-turn body of
    test_harness.main()'s while-loop verbatim, minus CLI print/input and
    EOFError/KeyboardInterrupt handling (CLI-only). Every step below calls
    into test_harness's / chat_store's existing public functions only.

    Mutates session.history, session.station_info (also mutated in place
    by test_harness.resolve_question itself on each lookup_station hit),
    and session.turn_index. Returns the answer text.

    Matches the CLI's error-handling convention exactly: on any internal
    failure (Claude API error, SQL error, etc.) this returns
    "[error] {exc}" as the answer text rather than raising -- it does NOT
    raise for ordinary runtime failures. Callers who need to distinguish
    resolved / needs_clarification / error should check the returned
    string's "[error] " prefix for now (status is still recorded via
    chat_store.record_turn, just not returned).
    """
    resolved: dict = {}
    answer_text: str | None = None
    status = "error"

    try:
        resolved = test_harness.resolve_question(
            session.client,
            session.conn,
            question,
            testing=False,
            history=session.history,
            station_info=session.station_info,
            home_location=session.home_location,
        )

        if resolved.get("status") != "resolved":
            answer_text = resolved.get(
                "clarification_question", "I need more information to answer that."
            )
            status = resolved.get("status", "needs_clarification")
        else:
            answer_payloads = []
            for entry in resolved["queries"]:
                result = test_harness.call_answer_query(session.conn, entry)
                answer_payloads.append(
                    test_harness.build_answer_payload(
                        entry, result, session.station_info, session.temp_unit
                    )
                )
            answer_text = test_harness.generate_answer(
                session.client, question, answer_payloads, history=session.history
            )
            status = "resolved"
    except Exception as exc:  # noqa: BLE001 -- parity with test_harness.main()
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
        model=test_harness.CLAUDE_MODEL,
        error=answer_text if status == "error" else None,
        station_id=turn_station_id,
    )
    session.turn_index += 1

    session.history.append(
        {
            "question": question,
            "answer": answer_text,
            "query": resolved["queries"][0] if resolved.get("queries") else None,
        }
    )

    return answer_text
