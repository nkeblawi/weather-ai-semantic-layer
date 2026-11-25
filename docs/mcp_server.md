# MCP Server

`wx_mcp/` is an [MCP](https://modelcontextprotocol.io) server that exposes the same query layer as the Streamlit app to any MCP client, so the client's own model resolves the question instead of the app's skill. Its tools are task-shaped: each one takes only the fields its kind of question needs, so most invalid combinations can't be expressed.

## Tools

- `lookup_station`: resolves a place to a station; its `ghcn_id` goes to every other tool

- `list_stations`: the stations covered in one country; with no country, it names the countries covered and asks which one

- `summarize_days`, `extreme_days`, `count_days`, `record_days`, `calendar_day`, `first_last_date`, `events`, `compare_periods`: single-answer questions

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

`wx_mcp/requirements.txt` pins everything the server needs: its own dependencies and `wxcore`'s, at the same versions as the app's `requirements.txt`. If you upgrade one of those shared packages in the app, upgrade it here too. The server reads the same `.env` as the app and needs no Anthropic key. Run it from the repo root:

```bash
pip install -r wx_mcp/requirements.txt
pip install -e .
.venv/bin/python -m wx_mcp.server
```

It serves Streamable HTTP at `http://127.0.0.1:8000/mcp`, and prints its catalog, limits, sign-in and address when it starts. To connect Claude Code:

```bash
claude mcp add --transport http weather http://127.0.0.1:8000/mcp
```

## Settings

All settings come from the environment or `.env`. The server checks them at startup and refuses to start, naming every problem, if any are wrong.

| Setting | Default | What it does |
| --- | --- | --- |
| `WX_CATALOG` | required | `weather_dev` or `weather` (prod) |
| `DATABRICKS_SERVER`, `DATABRICKS_HTTP`, `DATABRICKS_CLIENT`, `DATABRICKS_SECRET` | required | the warehouse and its service principal; checked by name only |
| `WX_MCP_USER_PER_MINUTE` | 6 | calls per user per minute |
| `WX_MCP_USER_PER_DAY` | 50 | calls per user per day |
| `WX_MCP_TOTAL_PER_DAY` | 500 | calls per day from everyone, to protect the warehouse quota the app and ingest jobs share |
| `WX_MCP_BLOCKLIST` | off | a file of users refused every call, one per line; reread when it changes |
| `WX_MCP_ISSUER`, `WX_MCP_RESOURCE` | off | sign-in: set both or neither (see below) |
| `WX_MCP_HOST` | `127.0.0.1` | where the server listens; the Docker image sets `0.0.0.0` |
| `WX_MCP_PORT` | 8000 | the port it listens on |

Each call is logged to stderr as one JSON line with its user, tool, outcome and time taken. The log and the blocklist hold user IDs, so keep them out of commits, issues and docs.

## Sign-in

With `WX_MCP_ISSUER` and `WX_MCP_RESOURCE` set, every request needs a sign-in token, and the limits count per user. `WX_MCP_ISSUER` is the OAuth provider's issuer (WorkOS AuthKit); `WX_MCP_RESOURCE` is this server's MCP URL, which each token must name as its audience. Both must be https, except on localhost. Clients find the provider through the server and sign the user in themselves. Claude Code and MCP Inspector are tested; ChatGPT and claude.ai are next.

Without sign-in, every caller shares one set of limits and anyone who can reach the server can use it. Run it that way only on your own machine, and never behind a tunnel or on a public host.

## Docker

The `Dockerfile` and `.dockerignore` at the repo root build an image with only `wxcore/`, `wx_mcp/` and `pyproject.toml`; `.env` and the blocklist never go into it. The build fails if the server can't import, and the server runs as a user without root.

```bash
docker build -t weather-mcp .
docker run --rm --env-file .env -p 127.0.0.1:8000:8000 weather-mcp
```

- Publish the port on `127.0.0.1` as above, not as `-p 8000:8000`. Inside the container the server listens on `0.0.0.0`, which turns off the MCP SDK's automatic DNS-rebinding protection (it only applies to localhost), so the published port is what keeps it local.

- `--env-file` takes values as written, so don't quote them in `.env`.

- To use a blocklist, mount the file and point `WX_MCP_BLOCKLIST` at its path inside the container, for example `-v "$PWD/blocklist.txt:/srv/blocklist.txt:ro" -e WX_MCP_BLOCKLIST=/srv/blocklist.txt`.

## Limitations

- **One process.** The chart CSVs (the last 50 charts, each for one hour) and the call counts live in this process's memory. A restart drops them, and a second copy of the server would keep its own, so limits would multiply and CSV links would fail when a request lands on the other copy. Run a single instance until these move to shared storage.

- **The warehouse can be slow.** A cold Free Edition warehouse can take a while to answer the first call, and some clients may give up before it does.

## Why the app doesn't use these tools

Both front ends end in the same query dict, checked by the same `wxcore` validation, but they ask the model for it differently:

- **The app** knows its model and prompt. Claude reads the skill (`skills/SKILL.md`), looks up stations and dates with two tools, and submits the whole question as one query JSON (`submit_resolution`). The app checks it once before anything runs, and sends a failed check back to Claude to fix. One flat schema keeps it to 2-3 model calls a question and lets one prompt teach every kind of question.

- **The MCP server** can't know which model calls it or what it has read, and it has no skill to send. Each task-shaped tool carries its own instructions in its description and takes only the fields its question needs, so a model that has never seen the skill is less likely to build a wrong query.
