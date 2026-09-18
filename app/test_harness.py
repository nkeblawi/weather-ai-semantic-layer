"""
CLI for the weather-query-resolver skill: a local interactive chat loop over
the shared pipeline in app/pipeline.py (the Claude API calls, the Databricks
SQL connection, the tool-use loop, and answer generation all live there.
This script is just the terminal UI plus chat-history persistence).

Usage:
    python app/test_harness.py
    (then type your question when prompted)

    python app/test_harness.py --testing
    (same, but also prints the raw resolved JSON, tool-call debug info, and
    each answer_query result for debugging purposes)

See app/pipeline.py's module docstring for the .env variables this requires.
"""

import sys
import os
import json

import anthropic

from pipeline import (
    CLAUDE_MODEL,
    build_answer_payload,
    call_answer_query,
    generate_answer,
    get_databricks_connection,
    resolve_question,
)
from chat_store import (
    ensure_user,
    new_session_id,
    record_turn,
)

# ---------------------------------------------------------------------------
# Main application code
# ---------------------------------------------------------------------------

INTRO = (
    "This tool answers questions about past weather observations at specific "
    "sites or airports. This is historical data only, not forecasts. When asking "
    "a question, please specify the city or major airport you are nearest to."
    "\n"
)


def main():
    testing = "--testing" in sys.argv

    # Greet the user
    print(INTRO)
    print()

    # Initialize connections
    client = anthropic.Anthropic()
    conn = get_databricks_connection()

    # Identify the user (stub auth: WX_APP_USER env var, else prompt) and
    # open a session so every turn below is written to {WX_CATALOG}.app.*.
    username = (
        os.environ.get("WX_APP_USER")
        or input("Sign in with your username (email or handle): ").strip()
    )
    user_id, temp_unit, home_location = ensure_user(
        conn,
        username,
        email=os.environ.get("WX_APP_EMAIL"),
        display_name=os.environ.get("WX_APP_NAME"),
        temp_unit=os.environ.get("WX_APP_TEMP_UNIT"),
        home_location=os.environ.get("WX_APP_HOME_LOCATION"),
    )
    session_id = new_session_id()
    turn_index = 0
    # Prior turns of this session, held in memory and replayed to the model.
    # The chat_history table is the durable log; a future --resume would
    # rehydrate this from it.
    history: list[dict] = []
    # ghcn_id -> {ghcn_id, name, city}, accumulated across the session from
    # every lookup_station call so an inherited station stays nameable.
    station_info: dict = {}
    if testing:
        print(
            f"[debug] user_id={user_id} session_id={session_id} "
            f"temp_unit={temp_unit} home_location={home_location!r}"
        )

    # Loop until user types 'quit'
    while True:
        try:
            question = input(
                "What's a weather question you wanted to ask? (type 'quit' to exit)\n> "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if question.lower() == "quit":
            break
        if not question:
            print("No question entered, please try again.")
            continue

        resolved: dict = {}
        answer_text: str | None = None
        status = "error"
        interrupted = False

        try:
            print("\nChecking data...")
            resolved = resolve_question(
                client,
                conn,
                question,
                testing=testing,
                history=history,
                station_info=station_info,
                home_location=home_location,
            )

            if testing:
                print("Resolved JSON:")
                print(json.dumps(resolved, indent=2))

            if resolved.get("status") != "resolved":
                answer_text = resolved.get(
                    "clarification_question",
                    "I need more information to answer that.",
                )
                status = resolved.get("status", "needs_clarification")
                print(answer_text)
                print()
            else:
                print("All parameters resolved...")

                answer_payloads = []
                for entry in resolved["queries"]:
                    result = call_answer_query(conn, entry)
                    if testing:
                        print(
                            f"  [debug] {entry['metric']} {entry['aggregation']}: {result}"
                        )
                    answer_payloads.append(
                        build_answer_payload(entry, result, station_info, temp_unit)
                    )

                if testing:
                    print(
                        f"[debug] answer payloads: {json.dumps(answer_payloads, indent=2)}"
                    )

                print("Results found!\n")
                answer_text = generate_answer(
                    client, question, answer_payloads, history=history
                )
                status = "resolved"
                print(answer_text)
        except (EOFError, KeyboardInterrupt):
            print()
            interrupted = True
        except Exception as exc:  # noqa: BLE001
            answer_text = f"[error] {exc}"
            status = "error"
            print(answer_text)

        if interrupted:
            break

        # Denormalized site for this turn (analytics + a future --resume).
        turn_station_id = None
        if status == "resolved" and resolved.get("queries"):
            turn_station_id = resolved["queries"][0].get("station_id")

        record_turn(
            conn,
            user_id=user_id,
            session_id=session_id,
            turn_index=turn_index,
            prompt=question,
            response=answer_text,
            status=status,
            resolved_json=json.dumps(resolved) if resolved else None,
            model=CLAUDE_MODEL,
            error=answer_text if status == "error" else None,
            station_id=turn_station_id,
        )
        turn_index += 1

        history.append(
            {
                "question": question,
                "answer": answer_text,
                "query": resolved["queries"][0] if resolved.get("queries") else None,
            }
        )


if __name__ == "__main__":
    main()
