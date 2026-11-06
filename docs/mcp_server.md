# MCP Server

`wx_mcp/` is an [MCP](https://modelcontextprotocol.io) server that exposes the same query layer as the Streamlit app to any MCP client, so the client's own model resolves the question instead of the app's skill. Its tools are task-shaped: each one takes only the fields its kind of question needs, so most invalid combinations can't be expressed.

## Tools

- `lookup_station`: resolves a place to a station; its `ghcn_id` goes to every other tool

- `list_stations`: the stations covered in one country; with no country, it names the countries covered and asks which one

- `summarize_days`, `extreme_days`, `count_days`, `calendar_day`, `first_last_date`, `events`, `compare_periods`: single-answer questions

- `chart_over_time`, `chart_climatology`, `chart_count_days`, `chart_odds`, `chart_dates_by_year`, `chart_histogram`, `chart_climate_graph`, `chart_running_total`, `chart_station_difference`, `chart_climograph`, `chart_ranking`: return a PNG of the app's chart and the numbers behind it, plus a link to a CSV resource for charts with 24 or more points

The tools are built on `wxcore/tools.py` and `wxcore/defaults.py`, then run through the same validation and `answer_query` call as the app.

## Resources

A client or user reads these; a model only sees one if it's attached.

- `wx://stations`: the stations covered, one per line as place (code)

- `chart://{chart_id}/{name}`: a chart's data as CSV, linked from the chart result

## Prompts

A user starts these from the client's menu or slash commands; some clients don't support them.

- `examples`: example questions and charts to try

## Running it

`wx_mcp/requirements.txt` pins the server's own dependencies, on top of the app's `requirements.txt`. It reads the same `.env` as the app and won't start unless `WX_CATALOG` and the Databricks service principal settings are set; it needs no Anthropic key. Run it from the repo root:

```bash
pip install -r wx_mcp/requirements.txt
.venv/bin/python -m wx_mcp.server
```

It serves Streamable HTTP at `http://127.0.0.1:8000/mcp` (e.g. for the MCP Inspector).

## Why the app doesn't use these tools

Both front ends end in the same query dict, checked by the same `wxcore` validation, but they ask the model for it differently:

- **The app** knows its model and prompt. Claude reads the skill (`skills/SKILL.md`), looks up stations and dates with two tools, and submits the whole question as one query JSON (`submit_resolution`). The app checks it once before anything runs, and sends a failed check back to Claude to fix. One flat schema keeps it to 2-3 model calls a question and lets one prompt teach every kind of question.

- **The MCP server** can't know which model calls it or what it has read, and it has no skill to send. Each task-shaped tool carries its own instructions in its description and takes only the fields its question needs, so a model that has never seen the skill is less likely to build a wrong query.
