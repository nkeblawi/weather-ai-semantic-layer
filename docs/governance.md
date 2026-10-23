# Governance

Who and what can access the weather data in Unity Catalog, how prod and dev
are kept apart, and how to audit it. Grants were last exported on 2026-10-22.

Principals are named by role, not by email or application ID, because this
repo is public.

## 1. Principals

| Principal | What it is | Used by |
|---|---|---|
| **Owner** | The workspace user who owns both catalogs and every object in them | Interactive work, `databricks bundle deploy`, `scripts/deploy_sql.sh`, and the scheduled jobs (prod jobs run as the owner) |
| **App service principal** | An OAuth machine identity (client ID + secret) | The Streamlit app, through `wxcore.db` |
| **account users** | Databricks built-in group: every user in the account | Nothing in this project; it only holds default and metadata grants |

The owner has every privilege on the objects it owns without an explicit
grant, so the owner doesn't appear in the grant tables below.

## 2. Catalogs and schemas

| Catalog | Environment | Selected by |
|---|---|---|
| `weather` | prod | bundle target `prod`; `WX_CATALOG=weather` (Streamlit Cloud secrets) |
| `weather_dev` | dev | bundle target `dev` (the default); `WX_CATALOG=weather_dev` (local `.env`) |

Both catalogs have the same schemas:

| Schema | Holds | Written by |
|---|---|---|
| `raw` | Source data as loaded (GHCN, ACIS, EMSHR, station list, GeoNames) | Ingest jobs |
| `cleaned` | Converted, merged, and QC'd observations and stations; `places` (GeoNames) | Transform jobs |
| `analytics` | `observations`, `stations`, `station_aliases`, `places`, and the UC SQL functions | Transform jobs, `scripts/deploy_sql.sh` |
| `app` | `users`, `chat_history`, and `usage_daily` (application state, not weather data) | The app; the retention job (section 8) |

## 3. Grants

These are the grants that were issued directly (`inherited_from = NONE`).
Everything else in the export is inherited from them. Both catalogs have the
same grants unless noted.

| Grantee | Object | Privileges | Why |
|---|---|---|---|
| App service principal | catalog | `USE_CATALOG` | Enter the catalog; reads nothing by itself |
| App service principal | schema `analytics` | `USE_SCHEMA`, `SELECT`, `EXECUTE` | Call `answer_query` and `lookup_station`, read `stations` directly, and read the tables the functions use |
| App service principal | schema `app` | `USE_SCHEMA` | Enter the schema; reads nothing by itself |
| App service principal | table `app.users` | `SELECT`, `MODIFY` | Read and upsert users and their preferences |
| App service principal | table `app.chat_history` | `SELECT`, `MODIFY` | Count and insert chat turns |
| account users | catalog `weather` (prod only) | `BROWSE` | Lets account users see the catalog's metadata, not its data |
| account users | schema `information_schema` | `USE_SCHEMA`, `SELECT` | Databricks default on every catalog |

The service principal can't see `raw`, `cleaned`, or `default`. It has no
`CREATE`, `MANAGE`, or ownership on anything, so it can't create, alter, or
drop objects, and it can't grant privileges to anyone.

The grants on `analytics` are schema-level on purpose: they carry over to
functions and tables that are recreated, and the schema holds only public
weather data and read-only functions. The `app` tables are created once with
`IF NOT EXISTS`, so table-level grants on them stay in place.

These least-privilege grants replaced catalog-level `USE_SCHEMA` + `SELECT`
on 2026-10-22, in dev first and then prod. To go back, re-grant
`SELECT, USE SCHEMA ON CATALOG` to the service principal.

### Grants outside Unity Catalog

These live in the workspace, not in Unity Catalog, so the export above
doesn't include them:

- **SQL warehouse** (Serverless Starter Warehouse, the one in `DATABRICKS_HTTP`): the owner has `IS_OWNER`, the app service principal has `CAN_USE`, the `users` group has `CAN_USE` (Databricks default), and `admins` inherit `CAN_MANAGE`.
- **Jobs:** see section 5.

## 4. Function redeploys and grants

`CREATE OR REPLACE FUNCTION` drops the function and creates it again, which
removes every grant made on that function. The app keeps working after a
redeploy because `EXECUTE` is granted on the `analytics` schema, so every
function in it, new or recreated, inherits that grant. Don't replace the
schema-level grant with per-function grants.

A function called from inside another function (e.g. `answer_query` calling
`query_by_day`) isn't checked against the caller's privileges.

## 5. Jobs

All jobs are defined in `resources/*.yml` and deployed with
`databricks bundle deploy -t dev|prod`. Each task gets the target catalog as
`--catalog`, so a job only reads and writes the catalog of its target.

| Job | Does | Writes | Prod trigger | Failure email |
|---|---|---|---|---|
| `wx_obs_ingest_ghcn` | Pulls new GHCN daily observations | `raw` | Daily 23:00 ET | Yes |
| `wx_obs_ingest_acis` | Pulls recent ACIS observations | `raw` | Daily 06:00 ET | Yes |
| `wx_obs_transform` | Converts and QCs GHCN and ACIS, merges, pivots, and adds derived variables | `cleaned`, `analytics.observations` | Daily 23:45 ET | Yes |
| `wx_station_ingest` | Fetches, filters, and merges the EMSHR and GHCN station lists | `raw`, `cleaned` | On demand | Yes (and on success) |
| `wx_activate_station` | Activates a station, recreates the `stations` view, and backfills its observations | `raw`, `cleaned`, `analytics` | On demand (job parameters) | No |
| `wx_load_station_history` | Loads observations from a station's older IDs (has `dry_run`) | `raw.obs_ghcn` | On demand (job parameters) | No |
| `wx_places_ingest` | Loads the GeoNames gazetteer (places with 1,000+ people) and recreates the `places` view | `raw.geonames_raw`, `cleaned.places`, `analytics.places` | On demand only | Yes |
| `wx_app_schema` | Creates the `app` schema and tables (`IF NOT EXISTS`) | `app` | On demand, after a deploy | Yes |
| `wx_app_retention` | Rolls up daily usage, then deletes old turns (section 8) | `app` | Daily 03:00 ET | Yes |

**Identity.** No job sets `run_as`, so every job runs as the user who
deployed it: the owner, in both targets. Jobs therefore have the owner's
full rights on both catalogs, not just their target's. The `--catalog`
argument is what keeps a job in its own catalog, not a grant.

**Permissions.** In prod, the owner has `IS_OWNER` (and `CAN_MANAGE` from
`databricks.yml`), and the workspace `admins` group has `CAN_MANAGE`. Dev
jobs are prefixed `[dev <user>]` by development mode and belong to the user
who deployed them.

**Dev.** The four scheduled jobs are paused in the dev target
(`databricks.yml`), so dev only changes when a job is run by hand.

**The app never runs jobs.** The app service principal has no job
permissions and no write access to `raw`, `cleaned`, or `analytics`.

## 6. Keeping prod and dev apart

- **Code** never hard-codes a catalog. `wxcore.db.catalog()` reads `WX_CATALOG` on each call and raises an error if it isn't set, so a missing setting can't fall back to prod.
- **SQL files** in `sql/uc/` are written against `weather.analytics.*`. `scripts/deploy_sql.sh --env dev` rewrites them to `weather_dev`, and `--env prod` asks for confirmation before deploying.
- **Jobs** get the catalog from the bundle target (`--catalog`). Dev schedules are paused.

## 7. Secrets

| Secret | Local | Streamlit Cloud |
|---|---|---|
| Databricks host, warehouse HTTP path | `.env` | App secrets |
| Service principal client ID and secret | `.env` | App secrets |
| Anthropic API key | `.env` | App secrets |

- `.env` is gitignored, so secrets never go in the repo.
- The app authenticates as the service principal with OAuth (client ID and secret), not with a personal access token.
- The deploy tooling (`databricks` CLI, `deploy_sql.sh`) authenticates as the owner through the CLI profile, not as the service principal.

## 8. User data and retention

The `app` schema is the only place the project stores data about people.

| Table | Holds | Kept |
|---|---|---|
| `app.users` | user_id, username, email, display name, units, home city, first/last seen | Named users only, indefinitely. Anonymous visitors aren't saved; their preferences last for the session |
| `app.chat_history` | Every turn: prompt, response, resolver JSON, model, status, error, station | 14 days |
| `app.usage_daily` | Turns, distinct users, and distinct sessions per UTC day and status; no content or ids | Indefinitely |

**How it runs.** The `wx_app_retention` job runs `src/wx/app/retention.py`
daily at 03:00 ET in prod (paused in dev). Each run, in order:

1. Adds every completed day still in `chat_history` to `usage_daily`, skipping days already there.
2. Deletes turns older than 14 days.
3. Runs `REORG TABLE ... APPLY (PURGE)` and `VACUUM` on `chat_history`, so deleted rows are removed from storage, not just hidden. `VACUUM` removes files after Delta's 7-day retention window, so a deleted row is gone from storage (and from time travel) within about 7 days of being deleted.

The app only reads today's turns (the daily question cap), so the
retention period doesn't change its behavior.

**Where questions go.** Every question is sent to Anthropic's API (Claude)
to be resolved and phrased. Nothing else leaves Databricks.

**Sign-in.** There's no login yet (`app/chat_store.py`): named users come
from `WX_APP_USER` locally, and public visitors get an anonymous username.

## 9. Known gaps

These are open observations, not changes that have been made:

- **One service principal for both catalogs.** Credentials used in local dev can also read and write prod. A separate dev principal would remove that.
- **`BROWSE` for account users** is granted on `weather` but not `weather_dev`.
- **Jobs run as the owner.** A dev job with a wrong `--catalog` could write to prod. A jobs service principal with write grants on its own catalog only (set as `run_as` per target) would make that impossible.
- **The on-demand station jobs have no failure email** (`wx_activate_station`, `wx_load_station_history`). They're run by hand, so a failure is seen in the run, but an alert would match the other jobs.

## 10. Auditing

Run this in the SQL editor as the owner to list every grant on both catalogs:

```sql
SELECT 'catalog' AS level, catalog_name AS object, grantee, privilege_type, inherited_from
FROM system.information_schema.catalog_privileges
WHERE catalog_name IN ('weather', 'weather_dev')
UNION ALL
SELECT 'schema', catalog_name || '.' || schema_name, grantee, privilege_type, inherited_from
FROM system.information_schema.schema_privileges
WHERE catalog_name IN ('weather', 'weather_dev')
UNION ALL
SELECT 'table', table_catalog || '.' || table_schema || '.' || table_name, grantee, privilege_type, inherited_from
FROM system.information_schema.table_privileges
WHERE table_catalog IN ('weather', 'weather_dev')
UNION ALL
SELECT 'function', specific_catalog || '.' || specific_schema || '.' || specific_name, grantee, privilege_type, inherited_from
FROM system.information_schema.routine_privileges
WHERE specific_catalog IN ('weather', 'weather_dev')
UNION ALL
SELECT 'volume', volume_catalog || '.' || volume_schema || '.' || volume_name, grantee, privilege_type, inherited_from
FROM system.information_schema.volume_privileges
WHERE volume_catalog IN ('weather', 'weather_dev')
ORDER BY level, object, grantee;
```

To see only the grants that were issued directly, add
`WHERE inherited_from = 'NONE'` around the query. Workspace permissions come
from the CLI:

```bash
databricks warehouses list
databricks warehouses get-permissions <warehouse-id>
databricks jobs get-permissions <job-id>
```

Update section 3 whenever the explicit grants change.
