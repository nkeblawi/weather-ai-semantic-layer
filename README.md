# Weather AI Semantic Layer

A natural-language interface for historical weather data. Ask a question like
*"What was the average temperature at Dulles in December 2022?"* and get back
a plain-English answer, backed by a Databricks data pipeline and Unity Catalog
query layer rather than general-purpose text-to-SQL conversions.

## Try the app

The chat app is live at **https://weather-obs-chat.streamlit.app/**.

Access is **invite-only**. To get in:

1. Ask the project owner for an invite, giving the email address you'll sign
   in with.
2. Accept the invitation email from Streamlit.
3. Open the app URL and sign in with that same email address.

Each session gets 10 questions; type `/help` in the chat for example
questions.

## Architecture

![AI Weather Chat architecture](images/AI%20Weather%20Chat%20Architecture.png)

- **Ingest → Transform**: NOAA station data arrives through a daily API
  ingest plus ad-hoc bulk downloads, and Databricks jobs clean and merge it
  into Delta Lake analytics tables.
- **Semantic layer**: Claude never writes free-form SQL. The skill gives it
  the weather definitions and schema, so it turns a question into structured
  parameters; the app then calls fixed Unity Catalog SQL functions with them.
- **User interface**: Streamlit sends the question in, and a second Claude
  call turns the query result into a plain-English answer.
- Everything runs on Databricks Free Edition and Streamlit Community Cloud,
  with no hyperscaler account.

The project has four layers:

## 1. Data Pipeline

Path: `src/wx/`

Ingests, cleans, and merges historical weather data on Databricks:

- **Stations**: fetches station lists from GHCN and EMSHR datasets, filters them
  down to WMO stations, and merges them into a single `station_merged` table with 
  ICAO/FAA codes and country info attached.
  
  Station activation (which stations the app can answer questions about) is a
  manual, on-demand step (`activate_station.py`) run by the application admin.

- **Observations**: ingests daily observations from GHCN and ACIS, converts each 
  source to a common schema, merges them, pivots to one row per station/day, and 
  derives additional variables (e.g. heating/cooling degree days) into a final 
  analytics table.

Runs as a set of Databricks Jobs, defined and deployed as a
[Databricks Asset Bundle](https://docs.databricks.com/dev-tools/bundles/index.html)
(`databricks.yml`, `resources/*.yml`).

## 2. Unity Catalog Query Layer

Path: `functions/sql/`

SQL functions deployed in Unity Catalog that answer one resolved query at a
time against the analytics tables:

- `lookup_station` — resolves free text (city, airport code, alias) to a
  station
- `query_by_day` / `query_by_event` / `query_by_period` — answer a query at
  the daily, event (e.g. a rain event), or monthly/yearly grain
- `answer_query` — the single entry point the app calls; dispatches to
  whichever of the three above applies

These are version-controlled here as a local mirror of what's deployed; 
kept in sync via a small CI/CD export tool.

### Rankings (`top` / `bottom`)

**Note: Functionality coming soon.**

`answer_query` and the three `query_by_*` functions accept two ranking
aggregations, `top` (largest first) and `bottom` (smallest first), plus an
optional `p_limit`:

- **Ranks** Ranking uses `DENSE_RANK`, so tied values share a rank
  and the next distinct value takes the next rank (1, 2, 2, 3). `p_limit` is
  the number of *ranks* returned, so "top 3" can return more than 3 rows when
  there are ties.
- **Limits** `p_limit` defaults to 3 and is clamped to 1-15. The returned
  list is also capped at 15 rows; `rows_truncated: true` means ties pushed
  more rows into those ranks than were returned.
- **Tie detection** compares values rounded to 2 decimals, so floating-point
  noise (53.300000000000004 vs 53.3) doesn't split a tie.
- **Result shape** Ranking results come back in a `rows` array (ordered by
  rank, then date) with `value` null; every other aggregation returns
  `rows: null`, so existing callers are unaffected.
  - day: `{rank, obs_date, value}`
  - event: `{rank, event_start, event_end, value, duration, total}`
  - month/year: `{rank, bucket_year, bucket_month, value, n_days}`

#### Coverage rule (month/year rankings)

A month or year is ranked only if at least **90%** of its calendar days have a
non-null value for the metric (`n_days >= 0.9 * days in the month/year`;
28 of 31 for a January, 329 of 365 for a year).

Why: a bucket's value is built from whatever days exist, so incomplete
buckets look artificially small (sums) or skewed (means). Without the rule,
the current partial year, a station's first partial year of record, and
months with reporting gaps crowd the top of `bottom` rankings, e.g. Dulles
1960 has only 122 days of snow data and would rank as the least snowy year.

Scope: the rule applies to `top`/`bottom` only. `max`/`min` are unchanged and
can still pick a partial bucket (Dulles `min` yearly snow currently returns
1960 at 0.0"). Event and day rankings don't need it, since an event or a day
either has data or it doesn't.

## 3. Claude Skills

Path: `skills/`

A [Claude Skill](https://docs.claude.com/en/docs/agents-and-tools/agent-skills/overview)
(`SKILL.md`) that defines, step by step, how a model turns a natural-language
weather question into the structured parameters (station, metric,
aggregation, date range) needed to call the query layer above.

Supporting files:

- `references/count_group.md`: rules and worked examples for events
  (storms, rain spells), month/year aggregates, and climate averages such
  as "average January high". The app adds it to the system prompt only when
  the question needs it, to keep the base prompt small.
- `references/answer_phrasing.md`: the prompt for the second Claude call,
  which turns query results into a 1-2 sentence answer.
- `assets/obs_schema.md`, `assets/stations_schema.md`: column-level schemas
  of the `observations` and `stations` tables, the semantic context behind
  each metric and station field.
- `scripts/resolve_date_range.py`: deterministic date math (seasons,
  "last Friday", month lengths, leap years). The app exposes it to Claude as
  a tool so the model never does calendar arithmetic itself.

## 4. Front-end Application

Path: `app/`

Ties the above together: a real Claude API call runs the skill to resolve a
question into parameters (calling `lookup_station` and a local date-range
resolver as tools along the way), the resolved parameters are used to call
`answer_query` in Unity Catalog, and a second, smaller Claude call turns the
numeric result into a natural-language answer in the same voice as the
original question. `app/test_harness.py` is a terminal CLI over this
pipeline for local testing; `app/chat_session.py` is the session wrapper
behind the Streamlit chat UI (`app/streamlit_app.py`), with input and usage
guardrails in `app/guardrails.py`. All of them import the same functions from
`app/pipeline.py` rather than duplicating any of this logic.

## Environments

The Databricks Asset Bundle deploys to two targets, each pointing at a
separate Unity Catalog catalog so pipeline changes can be tested without
touching production data:

- `dev` → `weather_dev`
- `prod` → `weather`
