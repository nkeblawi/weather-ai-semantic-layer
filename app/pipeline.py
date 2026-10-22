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

from wxcore.dates import resolve_date_range
from wxcore.db import call_lookup_station
from wxcore.db import fill_station_info as _fill_station_info

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The app's settings: the repo's .env locally (Streamlit Cloud sets them as
# secrets instead). Loaded by path, so it's found from any working
# directory; wxcore reads them when it's called.
load_dotenv(PROJECT_ROOT / ".env")
SKILL_PATH = PROJECT_ROOT / "skills" / "SKILL.md"
COUNT_GROUP_PATH = PROJECT_ROOT / "skills" / "references" / "count_group.md"
ANSWER_PHRASING_PATH = PROJECT_ROOT / "skills" / "references" / "answer_phrasing.md"
PLOTTING_PATH = PROJECT_ROOT / "skills" / "references" / "plotting.md"

CLAUDE_MODEL = "claude-haiku-4-5-20251001"

# How many prior turns of this session to replay to the resolver.
MAX_CONTEXT_TURNS = 6


def fill_station_info(conn, resolved: dict, station_info: dict) -> None:
    """
    wxcore.db.fill_station_info on the resolver's queries: adds the real
    stations it used without calling lookup_station this session, so
    validate_resolved accepts them.
    """
    _fill_station_info(conn, resolved.get("queries"), station_info)


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
    phrasing_results = [_for_phrasing(r) for r in results]
    parts.append(f"Results:\n{json.dumps(phrasing_results, indent=2)}")
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
    system_prompt = build_system_prompt(question, history, station_info, home_location, plot_mode)
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
                # from_prose: guardrails.validate_resolved only shows this
                # text if it's short and reads as a question.
                return {
                    "status": "needs_clarification",
                    "clarification_question": text.strip(),
                    "from_prose": True,
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
            "Resolves free text (city, airport name/alias, ICAO or IATA code) to an "
            "active station. Returns {ghcn_id, name, city, state}, or nothing if there's no match."
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
            "season, relative_days, weekday, recent_month, all_time. Only include the fields "
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

# The trigger words live in skills/assets/count_group_triggers.txt (one regex
# fragment per line), so they can be edited next to the guide they load.
TRIGGERS_PATH = PROJECT_ROOT / "skills" / "assets" / "count_group_triggers.txt"
_TRIGGERS = [
    line.strip() for line in TRIGGERS_PATH.read_text().splitlines()
    if line.strip() and not line.lstrip().startswith("#")
]
TRIGGER_PATTERN = re.compile(rf"\b({'|'.join(_TRIGGERS)})\b", re.IGNORECASE)


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
    for turn in (history or [])[-MAX_CONTEXT_TURNS:]:
        if TRIGGER_PATTERN.search(turn.get("question", "")):
            return True
        if (turn.get("query") or {}).get("unit", "day") != "day":
            return True
    return False


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
    skill_text = SKILL_PATH.read_text()
    skill_text = re.sub(r"^---\n.*?\n---\n\n", "", skill_text, count=1, flags=re.DOTALL)
    if plot_mode:
        # Up front, not only after the long skill text: without it the model
        # sometimes applies SKILL.md's single-value rules to a chart request
        skill_text = (
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
            + skill_text
        )

    # plotting.md builds on count_group.md's season/snow_season units, so a
    # plot turn loads both
    if plot_mode or needs_count_group(question, history):
        skill_text += "\n\n---\n\n" + COUNT_GROUP_PATH.read_text()
    if plot_mode:
        skill_text += "\n\n---\n\n" + PLOTTING_PATH.read_text()

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
        turn_text += (
            "\n\n---\n\n## Conversation context\n\n"
            "Earlier turns in this session, oldest first; the last is the "
            'most recent resolved query. Apply the "Follow-up questions" '
            "rules above.\n\n" + "\n\n".join(blocks)
        )

    return [
        {"type": "text", "text": skill_text, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": turn_text},
    ]
