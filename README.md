# Weather AI Semantic Layer

A natural-language interface for historical weather data. Ask a question like
*"What was the average temperature at Dulles in December 2022?"* and get back
a plain-English answer, backed by a Databricks data pipeline and Unity Catalog
query layer rather than general-purpose text-to-SQL conversions.

## Try the app

The chat app is live at **https://weather-obs-chat.streamlit.app/**. It's
public: no account or sign-in needed.

Each session gets 20 questions, and the app as a whole answers up to 100 a
day. Type `/help` in the chat for example questions, and start a question
with `/plot` to get a chart (see [Charts](#charts)).

## Architecture

![AI Weather Chat architecture](docs/images/AI%20Weather%20Chat%20Architecture.png)

- **Ingest → Transform**: NOAA station data arrives through a daily API
  ingest plus ad-hoc bulk downloads, and Databricks jobs clean and merge it
  into Delta Lake analytics tables.
- **Semantic layer**: Claude never writes free-form SQL. The skill gives it
  the weather definitions and schema, so it turns a question into structured
  parameters; the app then calls fixed Unity Catalog SQL functions with them.
- **User interface**: Streamlit sends the question in, and a second Claude
  call turns the query result into a plain-English answer.
- **Charts**: a `/plot` question resolves the same way, but the SQL returns
  a series of points, and fixed Altair templates in `wxcore` draw it. Claude
  picks *what* to chart, never *how* it's drawn.
- **MCP server**: the same query layer is also exposed as MCP tools, so any
  MCP client's model can answer weather questions without the Streamlit app.
- Everything runs on Databricks Free Edition and Streamlit Community Cloud,
  with no hyperscaler account.

**Project folder structure:**

| Path | Contains |
|---|---|
| `app/` | Streamlit App (with a CLI testing harness) |
| `docs/` | project notes |
| `resources/` | Databricks Jobs as YML files |
| `scripts/` | Bash Operations: `deploy_sql.sh` (deploy the SQL functions), `backfill.sh` (re-run the observation pipeline from a date) |
| `skills/` | AI skills with references and assets |
| `sql/uc/` | Unity Catalog Functions |
| `sql/maintenance/` | one-off and cleanup SQL (QC flag cleanup, station codes) |
| `src/wx/` | Data Pipeline Scripts |
| `wx_mcp/` | MCP Server |
| `wxcore/` | Shared Core - used both by Streamlit and the MCP server |

## 1. Streamlit App

Path: `app/`

Ties everything together: a real Claude API call runs the
[skill](#5-claude-skills) to resolve a question into parameters (calling
`lookup_station` and a local date-range resolver as tools along the way),
the resolved parameters are used to call `answer_query` in Unity Catalog,
and a second, smaller Claude call turns the numeric result into a
natural-language answer in the same voice as the original question.

`app/` holds the Streamlit front end and everything Claude-specific:
`pipeline.py` (the resolver's tool loop, system prompt, and answer
phrasing), `guardrails.py` (usage limits and input checks), `chat_session.py`
and `streamlit_app.py` (the chat UI), `chat_store.py` (chat history), and
`test_harness.py` (a terminal CLI for local testing). Data access,
validation, and charting come from [`wxcore`](#10-shared-core).

To run it locally, `pip install -r requirements.txt` also installs `wxcore`
(an editable install, defined in `pyproject.toml`), and `.env` must set
`WX_CATALOG` (`weather` for prod, `weather_dev` for dev) along with the
Databricks service principal settings. Nothing defaults to production: a
missing `WX_CATALOG` stops with an error.

### Charts

Start a question with `/plot` to get a chart. A few examples:

- `/plot summer highs at Reagan National with a trend line`
- `/plot a climate graph for Dulles in 2024`
- `/plot accumulated snowfall at Dulles in 2024-25 vs 2009-10`
- `/plot the chance of measurable snow by week at Dulles`

All chart types, how normals and records are computed, and the limits on
points and series are in [docs/charts.md](docs/charts.md).

## 2. Documentation

Path: `docs/`

- [station_lineage.md](docs/station_lineage.md): where each station's data
  comes from (source, record start, long gaps) and what was done to it
  (QC decisions).
- [governance.md](docs/governance.md): principals, catalogs, grants, jobs,
  secrets, keeping prod and dev apart, and the retention policy for the
  app's chat history.
- [pipeline.md](docs/pipeline.md): what each part of the data pipeline does,
  and how stations and places are resolved.
- [query_layer.md](docs/query_layer.md): rankings, snow seasons, and the
  coverage rule in the Unity Catalog functions.
- [charts.md](docs/charts.md): `/plot` chart types, normals, records, and
  limits.
- [skill.md](docs/skill.md): the Claude skill's supporting files and prompt
  caching.
- [mcp_server.md](docs/mcp_server.md): the MCP server's tools and how to
  run it.
- [deploying.md](docs/deploying.md): environments, deploying changes, and
  backfills.
- `images/`: the architecture diagram above.

## 3. Databricks Jobs

Path: `resources/`

The pipeline runs as a set of Databricks Jobs, defined and deployed as a
[Databricks Asset Bundle](https://docs.databricks.com/dev-tools/bundles/index.html)
(`databricks.yml` at the repo root, one `resources/*.job.yml` per job) to a
`dev` and a `prod` target ([environments](docs/deploying.md#environments)).
Scheduled times are US Eastern:

| Job | Runs | Does |
|---|---|---|
| `wx_obs_ingest_acis` | daily, 6:00 am | ingests recent ACIS observations (US stations) |
| `wx_obs_ingest_ghcn` | daily, 11:00 pm | ingests GHCN daily observations |
| `wx_obs_transform` | daily, 11:45 pm | merges, pivots, and derives the analytics table |
| `wx_app_retention` | daily, 3:00 am | rolls up and deletes old chat history ([governance](docs/governance.md)) |
| `wx_station_ingest` | by hand | fetches, filters, and merges the GHCN/EMSHR station lists |
| `wx_activate_station` | by hand | makes a station answerable in the app |
| `wx_load_station_history` | by hand | loads a station's older GHCN IDs under its current one |
| `wx_places_ingest` | by hand | loads the GeoNames places gazetteer |
| `wx_app_schema` | by hand | creates the app's chat tables |

## 4. Operations Scripts

Path: `scripts/`

- `deploy_sql.sh --env dev|prod [function ...]`: deploys the
  [Unity Catalog functions](#6-unity-catalog-functions).
- `backfill.sh --date YYYY-MM-DD --env dev|prod`: re-runs the observation
  pipeline from a date through yesterday.

How to deploy each part (SQL, pipeline, app) and how a backfill runs are in
[docs/deploying.md](docs/deploying.md).

## 5. Claude Skills

Path: `skills/`

A [Claude Skill](https://docs.claude.com/en/docs/agents-and-tools/agent-skills/overview)
(`SKILL.md`) that defines, step by step, how a model turns a natural-language
weather question into the structured parameters (station, metric,
aggregation, date range) needed to call the
[query layer](#6-unity-catalog-functions). Its `references/` are loaded only
for the questions that need them, `assets/` holds the table schemas and
worked examples, and `scripts/` the date math. Each file is described in
[docs/skill.md](docs/skill.md).

## 6. Unity Catalog Functions

Path: `sql/uc/`

SQL functions deployed in Unity Catalog that answer one resolved query at a
time against the analytics tables:

- `lookup_station` — resolves free text (city, airport code, alias) to a
  station
- `query_by_day` / `query_by_event` / `query_by_period` — answer a query at
  the daily, event (e.g. a rain event), or monthly/yearly grain
- `query_series` — returns every point of a chart instead of one value:
  daily (a year or less), monthly, yearly, seasonal, and snow-season series
  (means, sums, or counts of days past a threshold), climatologies by
  month, week, season, and calendar day (with each day's records), odds
  (the share of years a condition occurs), and histograms of daily values
- `answer_query` — the single entry point the app calls; dispatches to
  whichever of the above applies

The function definitions live here and are deployed from the repo with
`scripts/deploy_sql.sh`. How rankings, snow seasons, and the 90% coverage
rule work is in [docs/query_layer.md](docs/query_layer.md).

## 7. Maintenance SQL

Path: `sql/maintenance/`

One-off and cleanup SQL, run by hand:

- `qc_flag_cleanup.sql`: sets already-loaded values that failed a NOAA
  quality check to NULL, the same rule the pipeline applies during
  conversion (see [Data Pipeline](#8-data-pipeline)).
- `station_codes.sql`: the source of truth for airport codes EMSHR doesn't
  have (e.g. Canadian stations), merged into `cleaned.station_codes`. Add a
  row, rerun it, then rerun `wx_station_ingest` so the codes reach
  `station_merged`.

## 8. Data Pipeline

Path: `src/wx/`

Ingests, cleans, and merges historical weather data on Databricks, each
script running as a task in one of the [Databricks Jobs](#3-databricks-jobs):
station lists from GHCN and EMSHR (`stations/`), daily observations from
GHCN and ACIS (`obs/`), the GeoNames places gazetteer (`places/`), and the
app's chat tables (`app/`). Values that failed a NOAA quality check are set
to NULL unless verified as real.

The app covers 43 stations, each the main airport weather station for a
major city (38 in the US, 5 in Canada), with records starting between 1933
and 1994. Any other town resolves to its nearest station, and the user is
asked before it's used. Each step, and how stations and places are resolved,
are in [docs/pipeline.md](docs/pipeline.md).

## 9. MCP Server

Path: `wx_mcp/`

An [MCP](https://modelcontextprotocol.io) server that exposes the same query
layer to any MCP client, through task-shaped tools (`lookup_station`,
`count_days`, `chart_over_time`, ...) built on `wxcore`. Run it from the
repo root:

```bash
.venv/bin/python -m wx_mcp.server
```

The full tool list and connection details are in
[docs/mcp_server.md](docs/mcp_server.md).

## 10. Shared Core

Path: `wxcore/`

The core shared by the Streamlit app and the MCP server. It takes
structured query parameters and returns data, values, or chart specs. It
never calls Claude, never imports Streamlit, and keeps no session state:
station, units, and dates are always explicit arguments.

- `db.py`: the Databricks connection, `lookup_station`, and `answer_query`
- `dates.py`: seasons, date labels, and the date-range resolver
- `units.py`: metric units and labels, and °F/°C conversion
- `answers.py`: answer payloads and ranking tables
- `validation.py`: the allowed values and limits a query must fit, and the
  checks that run before anything reaches the warehouse
- `defaults.py`: fills in fields a query leaves out (the same rules the
  skill teaches), so a caller can send only what the question states
- `tools.py`: the task-shaped query tools behind the MCP server, each
  turning one kind of question's arguments into a query
- `series.py`, `charts.py`: chart data and the Altair templates

## Known limitations

- **Station-derived normals.** Normals come from the station's raw GHCN-D
  observations, so they can differ slightly from NOAA's official normals,
  which include adjustments and gap-filling. Charts note this.
- **Trend lines on uncorrected data.** Trends include station artifacts
  (instrument changes, site moves) as well as climate signal.
- **Observation data falls one day behind.** Ingest processes on the backend
  run at different times of day for different data sources, so observations
  typically run a day behind meaning "yesterday's" observations may not yet
  be available. The GHCN ingest runs at 11 pm ET, after NOAA's daily update
  (around 8 pm ET), so GHCN data runs about 2 days behind. The 6 am ET ACIS
  ingest catches only the stations that have reported by then (often not
  the West Coast); the rest arrive with GHCN.
- **Non-US stations.** Their data comes from GHCN only (ACIS covers US
  stations), and NOAA receives it later, so non-US observations typically
  run 2-3 days behind. Values are shown in US units (°F, inches).
- **Denver snowfall before 2006.** Denver's record starts at the current
  airport in 1994, and GHCN has no usable snowfall for 1995-2005 (missing,
  or rejected by NOAA's quality checks). Snowfall averages and totals for
  those years are incomplete.
- **Revised GHCN values aren't picked up.** When NOAA revises a value that
  is already loaded (or flags it later), the analytics tables keep the old
  value. `sql/maintenance/qc_flag_cleanup.sql` handles newly flagged values only.
- **No teleconnections.** Teleconnections need to be ingested from
  different data source(s). This has not been developed and is outside of
  the current scope. That may change in the future, however.
