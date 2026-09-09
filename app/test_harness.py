"""
Local integration test harness for the weather-query-resolver skill.

Runs the real production code: a real Claude API call using SKILL.md as the
system prompt (with skills/references/count_group.md appended only when the
question needs it), a Databricks SQL connection for lookup_station and
answer_query, and resolve_date_range tool run locally in-process.

This matches the decided architecture: production is a separate app calling
an AI model directly via API rather than a Databricks-hosted AI agent, and
only the two tools whose data actually live in Unity Catalog:

- lookup_station
- answer_query

Usage:
    python app/test_harness.py
    (then type your question when prompted)

    python app/test_harness.py --testing
    (same, but also prints the raw resolved JSON, tool-call debug info, and
    each answer_query result for debugging purposes)

Requires (add to your .env, loaded via python-dotenv):
    ANTHROPIC_API_KEY
    DATABRICKS_SERVER
    DATABRICKS_HTTP     (SQL warehouse connection details page in Databricks)
    DATABRICKS_PAT      (personal access token or service principal token)

NOTE: the exact parameter-binding syntax below (":name" style, passed via
the `parameters=` dict to cursor.execute) is databricks-sql-connector's
native-parameters feature.
"""

import sys
import os
import re
import json
from datetime import date
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
import anthropic
from databricks import sql as databricks_sql

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILL_PATH = PROJECT_ROOT / "skills" / "SKILL.md"
COUNT_GROUP_PATH = PROJECT_ROOT / "skills" / "references" / "count_group.md"
ANSWER_PHRASING_PATH = PROJECT_ROOT / "skills" / "references" / "answer_phrasing.md"
SCRIPTS_PATH = PROJECT_ROOT / "skills" / "scripts"

CLAUDE_MODEL = "claude-haiku-4-5-20251001"

# Unity Catalog catalog holding the analytics functions/views this app calls.
# Defaults to prod; set WX_CATALOG=weather_dev in .env to run against dev.
CATALOG = os.environ.get("WX_CATALOG", "weather")

# Import the resolve_date_range code used by the skill directly
sys.path.insert(0, str(SCRIPTS_PATH))
from resolve_date_range import resolve as _resolve_date_range_core  # type: ignore # noqa: E402

# Chat-history persistence to Unity Catalog (same dir as this file).
from chat_store import (  # noqa: E402
    ensure_user,
    new_session_id,
    record_turn,
)

# ---------------------------------------------------------------------------
# Databricks calls the two tools whose data actually lives in Unity
# Catalog. answer_query is called by this app code directly, AFTER the
# model call returns; it is not a tool the model itself calls.
# ---------------------------------------------------------------------------


def get_databricks_connection():
    return databricks_sql.connect(
        server_hostname=os.environ["DATABRICKS_SERVER"],
        http_path=os.environ["DATABRICKS_HTTP"],
        access_token=os.environ["DATABRICKS_PAT"],
    )


def call_lookup_station(conn, location_text: str):
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {CATALOG}.analytics.lookup_station(:location_text)",
            parameters={"location_text": location_text},
        )
        row = cur.fetchone()
    if not row or row[0] is None:
        return None
    return json.loads(row[0])


def call_answer_query(conn, entry: dict):
    threshold = entry.get("threshold") or {}
    event_day_threshold = entry.get("event_day_threshold") or {}
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {CATALOG}.analytics.answer_query(
                p_station_id => :station_id,
                p_metric => :metric,
                p_aggregation => :aggregation,
                p_start_date => :start_date,
                p_end_date => :end_date,
                p_unit => :unit,
                p_threshold_operator => :threshold_operator,
                p_threshold_value => :threshold_value,
                p_event_day_threshold_operator => :event_day_threshold_operator,
                p_event_day_threshold_value => :event_day_threshold_value,
                p_event_value => :event_value,
                p_period_aggregation => :period_aggregation,
                p_month_filter => :month_filter
            )
            """,
            parameters={
                "station_id": entry["station_id"],
                "metric": entry["metric"],
                "aggregation": entry["aggregation"],
                "start_date": entry["start_date"],
                "end_date": entry["end_date"],
                "unit": entry.get("unit", "day"),
                "threshold_operator": threshold.get("operator"),
                "threshold_value": threshold.get("value"),
                "event_day_threshold_operator": event_day_threshold.get("operator"),
                "event_day_threshold_value": event_day_threshold.get("value"),
                "event_value": entry.get("event_value"),
                "period_aggregation": entry.get("period_aggregation"),
                "month_filter": entry.get("month_filter"),
            },
        )
        row = cur.fetchone()
    if not row or row[0] is None:
        return None
    return json.loads(row[0])


# ---------------------------------------------------------------------------
# Primary application tools
# ---------------------------------------------------------------------------


def resolve_date_range(payload: dict) -> dict:
    """
    Adapts the real resolve()'s (start, end) date tuple into the
    {"start_date", "end_date"} string shape this harness's tool-result
    protocol expects. All the actual date logic lives in
    skills/scripts/resolve_date_range.py.
    """
    start, end = _resolve_date_range_core(payload)
    return {"start_date": start.isoformat(), "end_date": end.isoformat()}


# Physical unit each metric is stored in (the pipeline converts to imperial).
_METRIC_UNITS = {
    "TAVG": "°F",
    "TMAX": "°F",
    "TMIN": "°F",
    "PRCP": "inches",
    "SNOW": "inches",
    "SNWD": "inches",
    "AWND": "mph",
    "HDD": "heating degree-days",
    "CDD": "cooling degree-days",
}
# When aggregation is `count`, the value counts analysis-grain things, not
# the metric -- so the unit is the plural grain word.
_COUNT_UNITS = {"day": "days", "event": "events", "month": "months", "year": "years"}

_TEMP_METRICS = {"TAVG", "TMAX", "TMIN"}

# How many prior turns of this session to replay to the resolver.
MAX_CONTEXT_TURNS = 6


def answer_units(entry: dict) -> str:
    """Physical unit the answer value should be stated in."""
    grain = entry.get("unit", "day")
    if entry["aggregation"] == "count":
        return _COUNT_UNITS.get(grain, "days")
    if grain == "event" and entry.get("event_value") == "duration":
        return "days"
    if entry.get("output_unit") == "C" and entry["metric"] in _TEMP_METRICS:
        return "°C"
    return _METRIC_UNITS.get(entry["metric"], "")


def convert_value(entry: dict, value):
    """
    Apply the query's output_unit to the raw value. Only Fahrenheit ->
    Celsius, and only for temperature metrics. A stddev is a spread, so it
    scales by 5/9 without the 32-degree offset; everything else passes
    through unchanged.
    """
    if value is None:
        return None
    if entry.get("output_unit") == "C" and entry["metric"] in _TEMP_METRICS:
        if entry["aggregation"] == "stddev":
            return value * 5 / 9
        return (value - 32) * 5 / 9
    return value


def build_answer_payload(
    entry: dict, result: dict | None, station_info: dict, temp_unit: str = "F"
) -> dict:
    """
    Packages one query's parameters + its answer_query result into the plain
    facts the answer-generation model needs. No phrasing decisions are made
    here. The model turns this (plus the original question) into the final
    sentence; this function just makes sure the value it's given is exactly
    the one that should appear, so it has nothing to compute.
    """
    # User's Celsius preference, unless the query already picked a unit.
    if (
        temp_unit == "C"
        and entry.get("output_unit") is None
        and entry["metric"] in _TEMP_METRICS
    ):
        entry = {**entry, "output_unit": "C"}

    station = station_info.get(entry["station_id"], {})
    value = convert_value(entry, result.get("value") if result else None)
    if value is not None:
        value = int(value) if entry["aggregation"] == "count" else round(value, 1)

    payload = {
        "place": station.get("city") or station.get("name") or entry["station_id"],
        "metric": entry["metric"],
        "aggregation": entry["aggregation"],
        "units": answer_units(entry),
        "start_date": entry["start_date"],
        "end_date": entry["end_date"],
        "value": value,
    }
    if result:
        for key in (
            "obs_date",
            "event_start",
            "event_end",
            "bucket_year",
            "bucket_month",
        ):
            if result.get(key) is not None:
                payload[key] = result[key]
    return payload


def generate_answer(client, question: str, results: list[dict], history=None) -> str:
    # answer_phrasing.md is loaded fresh; not appended to the resolver's
    # system prompt like count_group.md)
    parts = []
    if history:
        transcript = "\n".join(
            f'- User asked: "{h["question"]}"\n  You answered: {h["answer"]}'
            for h in history[-MAX_CONTEXT_TURNS:]
        )
        parts.append(f"Earlier in this conversation:\n{transcript}\n")
    parts.append(f"Original question: {question}\n")
    parts.append(f"Results:\n{json.dumps(results, indent=2)}")
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=512,
        system=ANSWER_PHRASING_PATH.read_text(),
        messages=[{"role": "user", "content": "\n".join(parts)}],
    )
    return "".join(
        block.text for block in response.content if block.type == "text"
    ).strip()


def resolve_question(
    client,
    conn,
    question: str,
    testing: bool = False,
    history: list | None = None,
    station_info: dict | None = None,
    home_location: str | None = None,
) -> dict:
    """
    Runs the Claude tool-use loop until the model returns its final JSON,
    and returns that JSON.

    station_info maps ghcn_id -> the lookup_station result for it. It is
    session-scoped: the caller passes the same dict every turn, and each
    lookup_station call this turn adds to it, so a later follow-up that
    inherits a station (no fresh lookup) can still name the place.

    history is this session's prior turns ({question, answer, query});
    it's rendered into the system prompt as a transcript so a follow-up can
    inherit parameters or refer back to an earlier result.
    """
    if station_info is None:
        station_info = {}
    system_prompt = build_system_prompt(question, history, station_info, home_location)
    messages: list[dict[str, Any]] = [{"role": "user", "content": question}]

    while True:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2048,
            system=system_prompt,
            tools=TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            text = "".join(
                block.text for block in response.content if block.type == "text"
            )
            if testing:
                print(f"[debug] stop_reason={response.stop_reason!r}")
                print(f"[debug] raw final response text:\n{text!r}\n")

            try:
                return json.loads(_extract_json(text))
            except json.JSONDecodeError:
                # SKILL.md's Step 6 defines a needs_clarification JSON shape
                # for exactly this case, but the model sometimes answers in
                # plain prose instead of following it (seen for vague
                # questions like "how's the weather been?"). Treat unparseable
                # final text as that prose *being* the clarification message,
                # rather than crashing on a question the skill already knows
                # how to represent.
                if testing:
                    print(
                        "[debug] final text wasn't JSON; treating it as a "
                        "clarification message"
                    )
                return {
                    "status": "needs_clarification",
                    "clarification_question": text.strip(),
                }

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            result = execute_tool(conn, block.name, block.input)
            if block.name == "lookup_station" and result:
                station_info[result["ghcn_id"]] = result
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                }
            )
        messages.append({"role": "user", "content": tool_results})


# ---------------------------------------------------------------------------
# Secondary in-app tools
# ---------------------------------------------------------------------------


def _extract_json(text: str) -> str:
    """
    Models sometimes add prose or markdown fencing around the JSON despite
    Step 6 saying not to (seen from claude-haiku-4-5: a "**Step 6:**" header
    followed by a fenced ```json block). Pull the JSON out defensively
    rather than requiring byte-perfect output. Prefers a fenced block
    wherever it appears in the text; falls back to the outermost {...}.
    """
    fence_match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if fence_match:
        return fence_match.group(1)
    if "{" in text and "}" in text:
        return text[text.index("{") : text.rindex("}") + 1]
    return text


# ---------------------------------------------------------------------------
# Tool definitions for the Claude API. Only lookup_station & resolve_date_range.
# The model needs both to complete the resolved JSON itself (Step 2 / Step 5).
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "lookup_station",
        "description": (
            "Resolves free text (city, airport name/alias, ICAO or FAA code) to an "
            "active station. Returns {ghcn_id, name, city}, or nothing if there's no match."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"location_text": {"type": "string"}},
            "required": ["location_text"],
        },
    },
    {
        "name": "resolve_date_range",
        "description": (
            "Resolves a classified time expression to {start_date, end_date} in "
            "YYYY-MM-DD, per weather-query-resolver Step 5. kind is one of: "
            "absolute_year, absolute_year_range, absolute_month, absolute_day, "
            "season, relative_days, all_time. Only include the fields that apply "
            "to the chosen kind."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "current_date": {"type": "string"},
                "kind": {
                    "type": "string",
                    "enum": [
                        "absolute_year",
                        "absolute_year_range",
                        "absolute_month",
                        "absolute_day",
                        "season",
                        "relative_days",
                        "all_time",
                    ],
                },
                "year": {"type": "integer"},
                "month": {"type": "integer"},
                "day": {"type": "integer"},
                "start_year": {"type": "integer"},
                "end_year": {"type": "integer"},
                "season": {
                    "type": "string",
                    "enum": ["winter", "spring", "summer", "fall", "autumn"],
                },
                "days": {"type": "integer"},
            },
            "required": ["current_date", "kind"],
        },
    },
]


def execute_tool(conn, name: str, tool_input: dict):
    if name == "lookup_station":
        return call_lookup_station(conn, tool_input["location_text"])
    if name == "resolve_date_range":
        return resolve_date_range(tool_input)
    raise ValueError(f"unknown tool: {name}")


# ---------------------------------------------------------------------------
# Whether the question needs count_group.md appended, applying Step 4's
# trigger rule in app code instead of as a model-invoked tool call;
# this is decided upfront, and applied to the system prompt.
# ---------------------------------------------------------------------------

TRIGGER_PATTERN = re.compile(
    r"\b(event|events|storm|storms|spell|spells|in a row|month|months|year|years)\b",
    re.IGNORECASE,
)


def needs_count_group(question: str) -> bool:
    return bool(TRIGGER_PATTERN.search(question))


def build_system_prompt(
    question: str,
    history: list | None = None,
    station_info: dict | None = None,
    home_location: str | None = None,
) -> str:
    today = date.today().isoformat()
    skill_text = SKILL_PATH.read_text()
    skill_text = re.sub(r"^---\n.*?\n---\n\n", "", skill_text, count=1, flags=re.DOTALL)
    skill_text = skill_text.replace("{{CURRENT_DATE}}", today)

    if needs_count_group(question):
        skill_text += "\n\n---\n\n" + COUNT_GROUP_PATH.read_text()

    # Fallback location when the question names none (SKILL.md Step 2).
    if home_location:
        skill_text += f'\n\n---\n\n## User home location\n\n"{home_location}"'

    # Replay recent turns as a transcript. SKILL.md's "Follow-up questions"
    # section tells the resolver how to use it: inherit from the most recent
    # Resolved query for an elliptical follow-up, or reconstruct an earlier
    # turn's query for a back-reference / unit conversion.
    recent = (history or [])[-MAX_CONTEXT_TURNS:]
    if recent:
        si = station_info or {}
        blocks = []
        for i, turn in enumerate(recent, 1):
            lines = [f"--- turn {i} ---", f'User asked: "{turn["question"]}"']
            query = turn.get("query")
            if query:
                lines.append("Resolved query: " + json.dumps(query))
                info = si.get(query.get("station_id")) or {}
                names = list(
                    dict.fromkeys(n for n in (info.get("name"), info.get("city")) if n)
                )
                if query.get("station_id") and names:
                    lines.append(
                        f"(station_id {query['station_id']} is "
                        + " / ".join(names)
                        + " -- one site, whatever name the user uses)"
                    )
            if turn.get("answer"):
                lines.append(f"Answer given: {turn['answer']}")
            blocks.append("\n".join(lines))
        skill_text += (
            "\n\n---\n\n## Conversation context\n\n"
            "Earlier turns in this session, oldest first; the last is the "
            'most recent resolved query. Apply the "Follow-up questions" '
            "rules above.\n\n" + "\n\n".join(blocks)
        )

    return skill_text


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
