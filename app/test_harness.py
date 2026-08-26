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
    each answer_query result -- for debugging, not the normal user-facing
    output)

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

# Import the resolve_date_range code used by the skill directly
sys.path.insert(0, str(SCRIPTS_PATH))
from resolve_date_range import resolve as _resolve_date_range_core  # type: ignore # noqa: E402

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
            "SELECT weather.analytics.lookup_station(:location_text)",
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
            """
            SELECT weather.analytics.answer_query(
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


def build_answer_payload(entry: dict, result: dict | None, station_info: dict) -> dict:
    """
    Packages one query's parameters + its answer_query result into the plain
    facts the answer-generation model needs. No phrasing decisions are made
    here. The model turns this (plus the original question) into the final
    sentence; this function just makes sure the value it's given is exactly
    the one that should appear, so it has nothing to compute.
    """
    station = station_info.get(entry["station_id"], {})
    value = result.get("value") if result else None
    if value is not None:
        value = int(value) if entry["aggregation"] == "count" else round(value, 1)

    payload = {
        "place": station.get("city") or station.get("name") or entry["station_id"],
        "metric": entry["metric"],
        "aggregation": entry["aggregation"],
        "unit": entry.get("unit", "day"),
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


def generate_answer(client, question: str, results: list[dict]) -> str:
    # answer_phrasing.md is loaded fresh (not appended to the resolver's
    # system prompt like count_group.md) -- it's the entire system prompt
    # for this separate call, made every time.
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=512,
        system=ANSWER_PHRASING_PATH.read_text(),
        messages=[
            {
                "role": "user",
                "content": (
                    f"Original question: {question}\n\n"
                    f"Results:\n{json.dumps(results, indent=2)}"
                ),
            }
        ],
    )
    return "".join(
        block.text for block in response.content if block.type == "text"
    ).strip()


def resolve_question(
    client, conn, question: str, testing: bool = False
) -> tuple[dict, dict]:
    """
    Runs the Claude tool-use loop until the model returns its final JSON.
    Returns (resolved_json, station_info), where station_info maps
    ghcn_id -> the lookup_station result for it, collected along the way
    so the final answer can mention a place name instead of a raw ID.
    """
    system_prompt = build_system_prompt(question)
    messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
    station_info: dict[str, dict] = {}

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
                return json.loads(_extract_json(text)), station_info
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
                return (
                    {"status": "needs_clarification", "clarification_question": text.strip()},
                    station_info,
                )

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


def build_system_prompt(question: str) -> str:
    today = date.today().isoformat()
    skill_text = SKILL_PATH.read_text()
    skill_text = re.sub(r"^---\n.*?\n---\n\n", "", skill_text, count=1, flags=re.DOTALL)
    skill_text = skill_text.replace("{{CURRENT_DATE}}", today)

    if needs_count_group(question):
        skill_text += "\n\n---\n\n" + COUNT_GROUP_PATH.read_text()

    return skill_text


# ---------------------------------------------------------------------------
# Main application code
# ---------------------------------------------------------------------------

INTRO = (
    "This tool answers questions about past weather observations at specific "
    "sites or airports. This is historical data only, not forecasts. When asking "
    "a question, please specify the city or major airport you are nearest to."
)


def main():
    testing = "--testing" in sys.argv

    print(INTRO)
    print()
    question = input("What's a weather question you wanted to ask?\n> ").strip()
    if not question:
        print("No question entered, exiting.")
        sys.exit(1)

    client = anthropic.Anthropic()
    conn = get_databricks_connection()

    print("\nChecking data...")
    resolved, station_info = resolve_question(client, conn, question, testing=testing)

    if testing:
        print("Resolved JSON:")
        print(json.dumps(resolved, indent=2))

    if resolved.get("status") != "resolved":
        print(
            resolved.get(
                "clarification_question", "I need more information to answer that."
            )
        )
        return

    print("All parameters resolved...")

    answer_payloads = []
    for entry in resolved["queries"]:
        result = call_answer_query(conn, entry)
        if testing:
            print(f"  [debug] {entry['metric']} {entry['aggregation']}: {result}")
        answer_payloads.append(build_answer_payload(entry, result, station_info))

    print("Results found!\n")
    print(generate_answer(client, question, answer_payloads))


if __name__ == "__main__":
    main()
