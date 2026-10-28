# MCP Server

`wx_mcp/` is an [MCP](https://modelcontextprotocol.io) server that exposes
the same query layer as the Streamlit app to any MCP client, so the
client's own model resolves the question instead of the app's skill. Its
tools are task-shaped: each one takes only the fields its kind of question
needs, so most invalid combinations can't be expressed.

## Tools

- `lookup_station`: resolves a place to a station; its `ghcn_id` goes to
  every other tool
- `summarize_days`, `extreme_days`, `count_days`, `calendar_day`,
  `first_last_date`, `events`, `compare_periods`: single-answer questions
- `chart_over_time`, `chart_climatology`: return a PNG of the app's chart,
  plus a link to a CSV resource (`chart://...`) for charts with many points

The tools are built on `wxcore/tools.py` and `wxcore/defaults.py`, then run
through the same validation and `answer_query` call as the app.

## Running it

Run it from the repo root (reads the same `.env` as the app):

```bash
.venv/bin/python -m wx_mcp.server
```

It serves Streamable HTTP at `http://127.0.0.1:8000/mcp` (e.g. for the MCP
Inspector). `wx_mcp/requirements.txt` pins the server's own dependencies.
