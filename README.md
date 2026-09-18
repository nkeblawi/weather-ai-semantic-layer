# Weather AI Semantic Layer

A natural-language interface for historical weather data. Ask a question like
*"What was the average temperature at Dulles in December 2022?"* and get back
a plain-English answer, backed by a Databricks data pipeline and Unity Catalog
query layer rather than general-purpose text-to-SQL conversions.

The project has four layers:

## 1. Data pipeline (`src/wx/`)

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

## 2. Unity Catalog query layer (`functions/sql/`)

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

## 3. The skill (`skills/`)

A [Claude Skill](https://docs.claude.com/en/docs/agents-and-tools/agent-skills/overview)
(`SKILL.md`) that defines, step by step, how a model turns a natural-language
weather question into the structured parameters (station, metric,
aggregation, date range) needed to call the query layer above.

## 4. The application (`app/pipeline.py`)

Ties the above together: a real Claude API call runs the skill to resolve a
question into parameters (calling `lookup_station` and a local date-range
resolver as tools along the way), the resolved parameters are used to call
`answer_query` in Unity Catalog, and a second, smaller Claude call turns the
numeric result into a natural-language answer in the same voice as the
original question. `app/test_harness.py` is a terminal CLI over this
pipeline for local testing; `app/chat_session.py` is a non-CLI session
wrapper for a future Streamlit chat UI. Both import the same functions from
`app/pipeline.py` rather than duplicating any of this logic.

## Environments

The Databricks Asset Bundle deploys to two targets, each pointing at a
separate Unity Catalog catalog so pipeline changes can be tested without
touching production data:

- `dev` → `weather_dev`
- `prod` → `weather`

---

Setup and running instructions to come.
