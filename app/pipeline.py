"""
Shared pipeline library for the weather-query-resolver skill.

Runs the real production code: a real Claude API call using SKILL.md as the
system prompt (with skills/references/count_group.md appended only when the
question needs it), a Databricks SQL connection for lookup_station and
answer_query, and resolve_date_range tool run locally in-process.

This matches the decided architecture: production is a separate app calling
an AI model directly via API rather than a Databricks-hosted AI agent, and
only the two tools whose data actually live in Unity Catalog:

- lookup_station
- answer_query

This module holds the Claude side: the resolver's tool loop, its system
prompt, and answer phrasing. The data access and result shaping live in the
wxcore package (shared with the MCP server); callers import those from
wxcore directly.

It has no CLI and no chat-history persistence of its own. Both
app/test_harness.py (a CLI) and app/chat_session.py (a non-CLI session
wrapper for the Streamlit chat UI) import these same functions, and each
does its own chat_store.py persistence around them.

Requires (add to your .env, loaded via python-dotenv):
    ANTHROPIC_API_KEY   (used by callers to construct the anthropic.Anthropic
                          client passed into resolve_question/generate_answer;
                          this module never imports the anthropic package
                          itself)
    DATABRICKS_SERVER, DATABRICKS_HTTP, DATABRICKS_CLIENT, DATABRICKS_SECRET
                        (see wxcore/db.py)
"""

import re
import json
from datetime import date
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from wxcore.dates import ALL_TIME_START, resolve_date_range
from wxcore.db import call_lookup_station
from wxcore.db import fill_station_info as _fill_station_info


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The app's settings: the repo's .env locally (Streamlit Cloud sets them as
# secrets instead). Loaded by path, so it's found from any working
# directory; wxcore reads them when it's called.
load_dotenv(PROJECT_ROOT / ".env")
SKILL_PATH = PROJECT_ROOT / "skills" / "SKILL.md"
COUNT_GROUP_PATH = PROJECT_ROOT / "skills" / "references" / "count_group.md"
ANSWER_PHRASING_PATH = PROJECT_ROOT / "skills" / "references" / "answer_phrasing.md"
PLOTTING_PATH = PROJECT_ROOT / "skills" / "references" / "plotting.md"
# Worked examples live as data, one set per file above; each file has a marker
# line where its set is rendered (skill_text below)
EXAMPLES_PATH = PROJECT_ROOT / "skills" / "assets" / "examples.json"
EXAMPLES_MARKER = "<!-- worked examples: {} (skills/assets/examples.json) -->"

CLAUDE_MODEL = "claude-haiku-4-5-20251001"

# The resolver and answer phrasing see the whole session's history: it lives
# in the browser tab's session and ends with it, and guardrails'
# SESSION_TURN_LIMIT already bounds its length. MCP clients keep their own.

# How many recent turns needs_count_group checks for a follow-up to an
# events/months/years question, so one early "snowiest winters" doesn't load
# count_group.md for the rest of the session.
COUNT_GROUP_LOOKBACK_TURNS = 10

# What a failed turn is replayed as, in place of its "[error] ..." text
ERROR_REPLAY = (
    "(This turn failed with an internal error; the user got no answer. "
    "Their next message may retry it.)"
)

# Added to the generic reply a rejected clarification was replaced with
GENERIC_REPLAY_NOTE = (
    " (A generic fallback: your own clarification wasn't shown. The user's "
    "next message answers this, so combine it with their earlier question.)"
)


RETRY_PROMPT = (
    "Your submission failed these checks:\n{problems}\n\n"
    "Fix only what they name and call submit_resolution again (Step 6). If no "
    "allowed query means what the user asked, submit needs_clarification saying "
    "so: never change the query's meaning (an invented threshold or cutoff) just "
    "to pass the checks."
)

# The resolver's final reply is this tool's input, never free text: with
# tool_choice "any" every response is a tool call, so a turn can only end in
# the Step 6 object (free-text replies used to slip through as clarifications)
SUBMIT_TOOL = "submit_resolution"

# Sent back for a tool call that shared a response with submit_resolution
NOT_RUN_RESULT = "Not run: submit_resolution was in the same reply."


# Tool definitions for the Claude API: lookup_station & resolve_date_range
# (Step 2 / Step 5) to build the resolved JSON, and submit_resolution to hand it over.

TOOLS = [
    {
        "name": "lookup_station",
        "description": (
            "Resolves free text (city, airport name/alias, ICAO or IATA code) to an "
            "active station. Returns {ghcn_id, name, city, state} for a match, also with "
            "nearest_to and miles when the place has no station of its own but one serves it "
            "(in a city of the same name, or the only one within 10 miles). Otherwise "
            "{match: none, nearby, ask_user}: ask the user ask_user."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "location_text": {"type": "string"},
                "latitude": {"type": "number", "description": "optional: for a place too small to be known by name"},
                "longitude": {"type": "number", "description": "optional: for a place too small to be known by name"},
            },
            "required": ["location_text"],
        },
    },
    {
        "name": "resolve_date_range",
        "description": (
            "Resolves a classified time expression to {start_date, end_date} in "
            "YYYY-MM-DD, per weather-query-resolver Step 5. kind is one of: "
            "absolute_year, absolute_year_range, absolute_month, absolute_day, "
            "season, relative_days, last_years, weekday, recent_month, all_time. Only include the fields "
            "that apply to the chosen kind."
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
                        "last_years",
                        "weekday",
                        "recent_month",
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
                "years": {"type": "integer"},
                "weekday": {
                    "type": "string",
                    "enum": [
                        "monday",
                        "tuesday",
                        "wednesday",
                        "thursday",
                        "friday",
                        "saturday",
                        "sunday",
                    ],
                },
            },
            "required": ["current_date", "kind"],
        },
    },
    {
        "name": SUBMIT_TOOL,
        "description": (
            "Your final reply for every turn: the Step 6 object as this tool's input. "
            "Call it exactly once, last, after any lookup_station / resolve_date_range "
            "calls have returned. The user never sees text you write outside it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "current_date": {"type": "string"},
                "status": {"type": "string", "enum": ["resolved", "needs_clarification", "off_topic"]},
                "queries": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "resolved only: one query object per station x metric (Step 6)",
                },
                "clarification_question": {
                    "type": "string",
                    "description": "needs_clarification only: the one question to show the user",
                },
                "reason": {"type": "string", "description": "needs_plot only (Step 4); omit otherwise"},
            },
            "required": ["status"],
        },
    },
]


# Whether the question needs count_group.md appended, applying Step 4's
# trigger rule in app code instead of as a model-invoked tool call;
# this is decided upfront, and applied to the system prompt.

# The trigger words live in skills/assets/count_group_triggers.txt (one regex
# fragment per line), so they can be edited next to the guide they load.
TRIGGERS_PATH = PROJECT_ROOT / "skills" / "assets" / "count_group_triggers.txt"
_TRIGGERS = [
    line.strip() for line in TRIGGERS_PATH.read_text().splitlines()
    if line.strip() and not line.lstrip().startswith("#")
]
TRIGGER_PATTERN = re.compile(rf"\b({'|'.join(_TRIGGERS)})\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------


def _for_phrasing(payload: dict) -> dict:
    """
    What the phrasing model sees of one payload. A ranking's lead-in names
    only #1 (the table under it shows the rest), so it gets just the rank-1
    rows plus how many rows the table holds -- it can't list rows it never
    sees, however the prompt is followed.
    """
    rows = payload.get("rows")
    if rows is None:
        return payload
    out = {k: v for k, v in payload.items() if k != "rows"}
    # table rows read "Summer 2024"; the sentence reads "summer 2024"
    out["rank_1"] = [
        {**r, "season": r["season"].lower()} if "season" in r else r
        for r in rows
        if r.get("rank") == 1
    ]
    out["table_rows"] = len(rows)
    return out


# --- Secondary in-app tools ---


def _retry_results(submitted: dict, problems: list[str]) -> list[dict]:
    """
    The tool results answering the failed submission's message: the
    problems for submit_resolution, a not-run note for any other call in it
    (every tool_use needs its tool_result).
    """
    retry = RETRY_PROMPT.format(problems="\n".join(f"- {p}" for p in problems))
    return [
        {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": retry if block.name == SUBMIT_TOOL else NOT_RUN_RESULT,
            "is_error": True,
        }
        for block in submitted["content"]
        if block.type == "tool_use"
    ]


def _station_question(resolved: dict, ask_user: str | None, found: bool) -> dict:
    """
    A turn whose place matched no station asks lookup_station's own
    question (its nearby stations, or the covered cities from the stations
    table), not the model's rewording of it.
    """
    if ask_user and not found and resolved.get("status") == "needs_clarification":
        return resolved | {"clarification_question": ask_user}
    return resolved


def _example_json(resolved: dict) -> str:
    """A resolver output as the skill shows it: one line, or one query per line."""
    queries = resolved.get("queries")
    if not queries:
        return json.dumps(resolved, ensure_ascii=False)
    head = json.dumps({k: v for k, v in resolved.items() if k != "queries"}, ensure_ascii=False)[:-1]
    lines = ",\n".join("  " + json.dumps(q, ensure_ascii=False) for q in queries)
    return f'{head}, "queries": [\n{lines}\n]}}'


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------


def fill_station_info(conn, resolved: dict, station_info: dict) -> None:
    """
    wxcore.db.fill_station_info on the resolver's queries: adds the real
    stations it used without calling lookup_station this session, so
    validate_resolved accepts them.
    """
    _fill_station_info(conn, resolved.get("queries"), station_info)


def replayed_answer(turn: dict) -> str:
    """
    A turn's answer as the model sees it: its replay_answer when the
    shown text would mislead (an error, a generic fallback), else the
    answer the user saw.
    """
    return turn.get("replay_answer") or turn.get("answer") or ""


# A question about records whose results hold none: told plainly, or the
# answer calls the hottest days of a summer "record highs"
RECORD_WORDS = re.compile(r"\brecords?\b|record-(breaking|setting)|all-time", re.IGNORECASE)
NOT_RECORDS_NOTE = (
    "Note: none of these results is a record. They cover only the period "
    "asked about, not the whole record, so don't call them records, say they "
    "rank near one, or compare them with other years."
)


def _holds_a_record(payload: dict) -> bool:
    """
    An extreme over the whole record: the highest/lowest values (a calendar
    date's record too), or first/last dates with their earliest/latest on
    record. A value the answer may call a record.
    """
    extremes = ("max", "min", "top", "bottom", "first_date", "last_date")
    return payload.get("aggregation") in extremes and payload.get("start_date") == ALL_TIME_START


def generate_answer(client, question: str, results: list[dict], history=None) -> str:
    # answer_phrasing.md is loaded fresh; not appended to the resolver's
    # system prompt like count_group.md)
    parts = []
    if history:
        transcript = "\n".join(
            f'- User asked: "{h["question"]}"\n  You answered: {replayed_answer(h)}'
            for h in history
        )
        parts.append(f"Earlier in this conversation:\n{transcript}\n")
    parts.append(f"Original question: {question}\n")
    phrasing_results = [_for_phrasing(r) for r in results]
    parts.append(f"Results:\n{json.dumps(phrasing_results, indent=2)}")
    # regular answers only (a chart's summaries have no aggregation)
    if (RECORD_WORDS.search(question) and results and all("aggregation" in r for r in results)
            and not any(_holds_a_record(r) for r in results)):
        parts.append(NOT_RECORDS_NOTE)
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
    plot_mode: str | None = None,
    conversation: list | None = None,
    problems: list[str] | None = None,
) -> dict:
    """
    Runs the Claude tool-use loop until the model calls submit_resolution,
    and returns that call's input (the Step 6 object).

    conversation, if given, holds this turn's messages (the question, tool
    calls, the model's submission) and is extended in place. To have the
    model fix a submission that failed validation, call again with the same
    conversation and the problems: they're sent back as the submission's
    tool result.

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
    system_prompt = build_system_prompt(question, history, station_info, home_location, plot_mode)
    messages: list[dict[str, Any]] = conversation if conversation is not None else []
    if problems and messages:
        messages.append({"role": "user", "content": _retry_results(messages[-1], problems)})
    else:
        messages.append({"role": "user", "content": question})

    ask_user, found = None, False  # lookup_station's no-match question; a station resolved this turn
    while True:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2048,
            system=system_prompt,
            tools=TOOLS,
            tool_choice={"type": "any"},
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})
        calls = [b for b in response.content if b.type == "tool_use"]

        if submit := next((b for b in calls if b.name == SUBMIT_TOOL), None):
            if testing:
                print(f"[debug] submission:\n{json.dumps(submit.input, indent=2)}\n")
            return _station_question(dict(submit.input), ask_user, found)
        if not calls:
            # tool_choice "any" rules this out unless the reply was cut off (max_tokens)
            raise RuntimeError(f"resolver ended without {SUBMIT_TOOL} (stop_reason={response.stop_reason!r})")

        tool_results = []
        for block in calls:
            result = execute_tool(conn, block.name, block.input)
            if block.name == "lookup_station" and result and "ghcn_id" in result:
                station_info[result["ghcn_id"]] = result
                found = True
            elif block.name == "lookup_station" and result:
                ask_user = result.get("ask_user")
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                }
            )
        messages.append({"role": "user", "content": tool_results})


def execute_tool(conn, name: str, tool_input: dict):
    if name == "lookup_station":
        return call_lookup_station(
            conn, tool_input["location_text"], tool_input.get("latitude"), tool_input.get("longitude")
        )
    if name == "resolve_date_range":
        return resolve_date_range(tool_input)
    raise ValueError(f"unknown tool: {name}")


def needs_count_group(question: str, history: list | None = None) -> bool:
    """
    True if this question -- or the recent conversation it may be following
    up on -- is about events/months/years. A follow-up like "narrow that to
    this winter" names no trigger word itself, so earlier turns count too:
    their questions (including ones that only got a clarification back) and
    any non-day query they resolved to.
    """
    if TRIGGER_PATTERN.search(question):
        return True
    for turn in (history or [])[-COUNT_GROUP_LOOKBACK_TURNS:]:
        if TRIGGER_PATTERN.search(turn.get("question", "")):
            return True
        if (turn.get("query") or {}).get("unit", "day") != "day":
            return True
    return False


def render_example(example: dict) -> str:
    """One worked example (an entry of examples.json) as Markdown."""
    line = f"**Q:** {example['question']}"
    if "note" in example:
        line += f" ({example['note']})"
    if "context" in example:
        line = f"{example['context']} — {line}"
    explanation = f"\n{example['explanation']}\n" if "explanation" in example else ""
    return f"{line}\n{explanation}```json\n{_example_json(example['resolved'])}\n```"


def skill_text(path: Path, examples_key: str) -> str:
    """A skill file with its set of worked examples rendered in at the marker."""
    examples = json.loads(EXAMPLES_PATH.read_text())[examples_key]
    return path.read_text().replace(
        EXAMPLES_MARKER.format(examples_key), "\n\n".join(render_example(e) for e in examples)
    )


def build_system_prompt(
    question: str,
    history: list | None = None,
    station_info: dict | None = None,
    home_location: str | None = None,
    plot_mode: str | None = None,
) -> list[dict]:
    """
    The resolver's system prompt as two blocks: the skill text (the same for
    every question of a kind, so it's cached -- repeat reads cost a tenth),
    then this turn's details (date, plot mode, home city, conversation),
    which must come after the cache point or every call would miss.
    """
    text = skill_text(SKILL_PATH, "skill")
    text = re.sub(r"^---\n.*?\n---\n\n", "", text, count=1, flags=re.DOTALL)
    if plot_mode:
        # Up front, not only after the long skill text: without it the model
        # sometimes applies SKILL.md's single-value rules to a chart request
        text = (
            "# PLOT MODE\n\n"
            + (
                "This turn is a `/plot` request. Follow `references/plotting.md` (appended below): every "
                "resolved query must use `aggregation: \"series\"`. If nothing can be charted, return "
                "`needs_clarification` asking what to chart. Never return `reason: \"needs_plot\"`.\n\n"
                if plot_mode == "plot"
                else "This turn follows a chart. Follow `references/plotting.md` (appended below): chart it "
                "with `series` if it asks for another chart, otherwise answer it as a regular question. "
                "Never return `reason: \"needs_plot\"`.\n\n"
            )
            + "---\n\n"
            + text
        )

    # plotting.md builds on count_group.md's season/snow_season units, so a
    # plot turn loads both
    if plot_mode or needs_count_group(question, history):
        text += "\n\n---\n\n" + skill_text(COUNT_GROUP_PATH, "count_group")
    if plot_mode:
        text += "\n\n---\n\n" + skill_text(PLOTTING_PATH, "plotting")

    # Per-turn details: after the cache point
    turn_text = f"## Today's date\n\n{date.today().isoformat()}"
    if plot_mode:
        turn_text += "\n\n---\n\n## Plot mode\n\n" + (
            "This is a `/plot` turn: the user asked for a chart (plotting.md Step 1)."
            if plot_mode == "plot"
            else "This is a follow-up to a chart (no `/plot`): chart it if it asks for another "
            "chart, otherwise answer it as a regular question (plotting.md Step 1)."
        )

    # Fallback location when the question names none (SKILL.md Step 2).
    if home_location:
        turn_text += f'\n\n---\n\n## User home city\n\n"{home_location}"'

    # Replay the session's turns as a transcript. SKILL.md's "Follow-up questions"
    # section tells the resolver how to use it: inherit from the most recent
    # Resolved query for an elliptical follow-up, or reconstruct an earlier
    # turn's query for a back-reference / unit conversion.
    recent = history or []
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
            if answer := replayed_answer(turn):
                # a clarifying question isn't an answer: the latest one is
                # waiting for the reply this turn may be (SKILL.md "Answer to
                # your own question")
                if turn.get("status") != "needs_clarification":
                    label = "Answer given"
                elif i == len(recent):
                    label = "You asked the user (awaiting their answer)"
                else:
                    label = "You asked the user"
                lines.append(f"{label}: {answer}")
            blocks.append("\n".join(lines))
        turn_text += (
            "\n\n---\n\n## Conversation context\n\n"
            "Earlier turns in this session, oldest first; a turn with a "
            "`Resolved query` was answered, one without was a question to the "
            'user. Apply the "Follow-up questions" rules above.\n\n'
            + "\n\n".join(blocks)
        )

    return [
        {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": turn_text},
    ]
