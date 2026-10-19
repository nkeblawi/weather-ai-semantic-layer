# Weather AI Semantic Layer

A natural-language interface for historical weather data. Ask a question like
*"What was the average temperature at Dulles in December 2022?"* and get back
a plain-English answer, backed by a Databricks data pipeline and Unity Catalog
query layer rather than general-purpose text-to-SQL conversions.

## Try the app

The chat app is live at **https://weather-obs-chat.streamlit.app/**. It's
public: no account or sign-in needed.

Each session gets 10 questions, and the app as a whole answers up to 100 a
day. Type `/help` in the chat for example questions, and start a question
with `/plot` to get a chart (see [Charts](#charts-plot)).

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
- **Charts**: a `/plot` question resolves the same way, but the SQL returns
  a series of points, and fixed Altair templates in the app draw it. Claude
  picks *what* to chart, never *how* it's drawn.
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
- `query_series` — returns every point of a chart instead of one value:
  daily (a year or less), monthly, yearly, seasonal, and snow-season series
  (means, sums, or counts of days past a threshold), climatologies by
  month, week, season, and calendar day (with each day's records), odds
  (the share of years a condition occurs), and histograms of daily values
- `answer_query` — the single entry point the app calls; dispatches to
  whichever of the above applies

These are version-controlled here as a local mirror of what's deployed; 
kept in sync via a small CI/CD export tool.

### Rankings (`top` / `bottom`)

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

#### Snow seasons

Snowfall grouped by winter ("the snowiest winters", "an average snow season")
uses the July 1 - June 30 snow season (the NWS convention), not the calendar
year, so one winter's snow isn't split across two years and October or May
snow still counts toward its season. `query_by_period`/`answer_query` take
`p_year_start_month` (default 1 = calendar year; the app passes 7 for snow
seasons), and a season is labeled by the year it ends in (2010 = "2009-10").

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
- `references/plotting.md`: rules and worked examples for `/plot`
  questions: picking the grain, normals and anomalies, trend lines, daily
  climate graphs, and running totals. Loaded only on chart turns.
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

The skill text is sent as a cached system-prompt block, with the date, home
city, and conversation after it, so the 2-3 resolver calls per question
re-read the skill at a tenth of the input cost.

### Charts

Command: `/plot`

Start a question with the `/plot` command. A chart question without it ("average high
and low for each month at Dulles") gets a **Plot it** button that resends
it as `/plot`, and a follow-up to a chart ("now do Reagan National") stays
a chart unless it asks for a single value. `app/series.py` turns the SQL
result into chart data (unit conversion, normals, trend fits) and
`app/charts.py` holds the Altair templates.

| Chart | Example |
|---|---|
| Time series, monthly / yearly / seasonal | `/plot summer highs at Reagan National with a trend line` |
| Counts of days per period | `/plot the number of 90-degree days per year at Dulles` |
| Climatology by month, season, or calendar day | `/plot average high and low for each month at Dulles` |
| Against normal (overlay or departure) | `/plot monthly temperatures at Dulles in 2024 against normal` |
| Daily values, a year or less | `/plot daily rainfall at Reagan National in 2023` |
| Daily climate graph (normals and records) | `/plot a climate graph for Dulles in 2024` |
| Running totals, several seasons overlaid | `/plot accumulated snowfall at Dulles in 2024-25 vs 2009-10` |
| Histograms (daily or per-period values) | `/plot a histogram of July high temperatures at Dulles` |
| Odds by month, week, season, or day | `/plot the chance of measurable snow by week at Dulles` |
| Ranked bars | `/plot the 10 biggest snowstorms at Dulles` |
| Station difference | `/plot how much colder Dulles lows are than Reagan National's, by year` |
| Climograph | `/plot a climograph for Reagan National` |
| Dates by year (freezes, first 90°F day), growing season | `/plot the last spring and first fall freeze at Dulles each year with a trend line` |

- **Normals** are computed from the station's own observations, 1991-2020
  by default (10-50 years allowed). Daily normals are smoothed over +/- 7
  days. **Records** cover the whole station record.
- **Limits**: daily points only over a year or less, monthly points up to
  100 years (yearly beyond), and at most 4 series per question. Longer
  requests are reduced, with a note under the chart.
- Charts with 2+ series have a **clickable legend** to hide or show each
  one; charts with many points offer a CSV download.

Single values use the same data without a chart: "the record high for July
4", "the normal low on Christmas" (normal = 1991-2020; "average" = the whole
record), "the odds of a white Christmas", "the last spring freeze in 2025", "when the first
measurable snow usually comes".

## Environments

The Databricks Asset Bundle deploys to two targets, each pointing at a
separate Unity Catalog catalog so pipeline changes can be tested without
touching production data:

- `dev` → `weather_dev`
- `prod` → `weather`

## Deploying changes

- **SQL functions:** `scripts/deploy_sql.sh --env dev|prod [function ...]`
  (add `--dry-run` to preview). Deploy to dev first, and deploy SQL before
  the app code that depends on it. `CREATE OR REPLACE FUNCTION` recreates
  the function and drops its grants, so after redeploying `answer_query` or
  `lookup_station`, re-grant `EXECUTE` on it to the app's service principal
  (or grant `EXECUTE` once on the `analytics` schema).
- **App:** Streamlit Community Cloud redeploys on every push to `main`, but
  it doesn't reliably reload changed Python modules (`app/guardrails.py`,
  `app/pipeline.py`, `app/chat_session.py`, `app/series.py`,
  `app/charts.py`); the running app keeps the old
  versions. After pushing changes to any of them, **reboot the app** from
  the Community Cloud dashboard. Skill files (`skills/`) are read on every
  question, so they take effect without a reboot.

## Known limitations

- **Partial periods in non-ranking aggregates.** The 90% coverage rule
  applies only to `top`/`bottom` rankings. `mean`, `count`, `max`, and `min`
  across months, years, and seasons still include partial buckets: an
  "average winter" includes the snow season under way, and the least snowy
  year (`min`) at Dulles comes back as 1960, which has only 122 days of
  data.
- **Ties in single answers.** When two days tie for `max`/`min`, which date
  is returned isn't guaranteed (e.g. July 16 or July 20, 1980, both 101°F at
  Dulles).
- **Rank ties vs. displayed values.** Ties are detected on values rounded
  to 2 decimals, but tables show 1 decimal, so two rows can both read
  77.4°F yet hold different ranks.
- **Display details.** Some ranking-table titles show the city without its
  state ("Sterling"), and answers capitalize season labels mid-sentence
  ("in Summer 2024").
- **Station-derived normals.** Normals come from the station's raw GHCN-D
  observations, so they can differ slightly from NOAA's official normals,
  which include adjustments and gap-filling. Charts note this.
- **Trend lines on uncorrected data.** Trends include station artifacts
  (instrument changes, site moves) as well as climate signal.
