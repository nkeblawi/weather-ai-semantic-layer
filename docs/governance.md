# Governance

Who and what can access the weather data in Unity Catalog, how prod and dev are kept apart, and how to audit it. Grants were last checked on 2026-11-14.

Principals are named by role, not by email or application ID, because this repo is public.

## 1. Principals

| Principal | What it is | Used by |
|---|---|---|
| **Owner** | The workspace user who owns both catalogs, their schemas, the SQL functions, the views the jobs don't recreate, and `station_aliases` | Interactive work, `databricks bundle deploy`, `scripts/deploy_sql.sh` |
| **Prod app service principal** | OAuth machine identity (client ID + secret) | The Streamlit Cloud app, through `wxcore.db` |
| **Dev app service principal** | OAuth machine identity (client ID + secret) | Local Streamlit, the test harness, tests, and the local MCP server |
| **Prod jobs service principal** | Machine identity with no secret | `run_as` for every prod job |
| **Dev jobs service principal** | Machine identity with no secret | `run_as` for every dev job |
| **account users** | Databricks built-in group: every user in the account | Nothing in this project; it holds only the default `information_schema` grants |

Each service principal has the Databricks SQL access entitlement and nothing else, except the jobs service principals, which also have workspace access to read the bundle's files and run jobs. None is a workspace admin.

The owner doesn't appear in the grant tables below for objects it owns. Owning a catalog or schema lets the owner manage everything in it (grant, alter, transfer ownership), but not read or write data in tables it doesn't own, so the owner also has explicit schema grants (section 3).

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
| `analytics` | `observations`, `stations`, `station_aliases`, `places`, and the UC SQL functions | Transform jobs; `scripts/deploy_sql.sh` (the functions and the `station_aliases` seed) |
| `app` | `users`, `chat_history`, and `usage_daily` (application state, not weather data) | The app; the retention job (section 8) |

## 3. Grants

These are the grants issued directly (`inherited_from = NONE`); everything else is inherited from them. Each service principal is granted on its own catalog only: the prod ones on `weather`, the dev ones on `weather_dev`. Both catalogs have the same grants unless noted.

This is based on LEAST PRIVILEGE for each role.

**App service principals** (read the serving layer, write application state):

| Object | Privileges | Why |
|---|---|---|
| catalog | `USE_CATALOG` | Enter the catalog; reads nothing by itself |
| schema `analytics` | `USE_SCHEMA`, `SELECT`, `EXECUTE` | Call `answer_query` and `lookup_station`, read `stations` and `places` directly, and read the tables the functions use |
| schema `app` | `USE_SCHEMA` | Enter the schema; reads nothing by itself |
| table `app.users` | `SELECT`, `MODIFY` | Read and upsert users and their preferences |
| table `app.chat_history` | `SELECT`, `MODIFY` | Count and insert chat turns |

An app service principal can't see `raw`, `cleaned`, `default`, or `app.usage_daily`. It has no `CREATE`, `MANAGE`, or ownership on anything, so it can't create, alter, or drop objects, and it can't grant privileges to anyone.

**Jobs service principals** (write the pipeline's tables):

| Object | Privileges | Why |
|---|---|---|
| catalog | `USE_CATALOG` | Enter the catalog |
| schemas `raw`, `cleaned`, `analytics`, `app` | `USE_SCHEMA`, `SELECT`, `MODIFY`, `CREATE_TABLE` | Read, merge into, and create tables and views |
| views `analytics.stations`, `analytics.places` | `MANAGE` | Replace the views (`CREATE OR REPLACE VIEW`) |
| every table the jobs write (20: all of `raw`, `cleaned` and `app`, and `analytics.observations`) | ownership | Overwrite, `ALTER TABLE`, and `VACUUM` need ownership |

`MANAGE` is used on the views because a non-admin can transfer a view only to a group it belongs to. Once a job recreates a view, the jobs service principal owns it. The jobs service principals have no `CREATE SCHEMA`, `CREATE FUNCTION`, or volume privileges.

**Owner** (explicit, despite owning the catalogs):

| Object | Privileges | Why |
|---|---|---|
| schemas `raw`, `cleaned`, `analytics`, `app` | `ALL_PRIVILEGES` | The owner no longer owns the job tables, and the views it owns run with its rights. Without `SELECT` here, `stations` (and the app's station lookup, the dashboards, and the lag alert) fail |

Grant the owner's schema privileges before transferring table ownership, never after.

**account users:** only the Databricks default `USE_SCHEMA` and `SELECT` on `information_schema`. `BROWSE` was revoked on `weather` (and on two other projects' catalogs in the workspace) on 2026-11-14, so other identities don't even see the prod catalog listed.

The grants on `analytics` are schema-level on purpose: they carry over to functions and tables that are recreated, and the schema holds only public weather data and read-only functions. The `app` tables are created once with `IF NOT EXISTS`, so table-level grants on them stay in place.

### Grants outside Unity Catalog

These live in the workspace, not in Unity Catalog, so the export above doesn't include them:

- **SQL warehouse** (Serverless Starter Warehouse, the one in `DATABRICKS_HTTP`): all four service principals have `CAN_USE`, the owner has `IS_OWNER`, the `users` group has `CAN_USE` (Databricks default), and `admins` inherit `CAN_MANAGE`.

- **Jobs service principals:** the owner has the **Service Principal: User** role on each (needed to deploy a job with that `run_as`), not Manager, so nobody creates secrets for them by accident.

- **Jobs:** see section 5.

## 4. Function redeploys and grants

`CREATE OR REPLACE FUNCTION` drops the function and creates it again, which removes every grant made on that function. The app keeps working after a redeploy because `EXECUTE` is granted on the `analytics` schema, so every function in it, new or recreated, inherits that grant. Don't replace the schema-level grant with per-function grants.

A function called from inside another function (e.g. `answer_query` calling `query_by_day`) isn't checked against the caller's privileges.

## 5. Jobs

All jobs are defined in `resources/*.yml` and deployed with `databricks bundle deploy -t dev|prod`. Each task gets the target catalog as `--catalog`, so a job only reads and writes the catalog of its target.

| Job | Does | Writes | Prod trigger | Failure email |
|---|---|---|---|---|
| `wx_obs_ingest_ghcn` | Pulls new GHCN daily observations | `raw` | Daily 23:00 ET | Yes |
| `wx_obs_ingest_acis` | Pulls recent ACIS observations | `raw` | Daily 18:00 ET | Yes |
| `wx_obs_transform` | Converts and QCs GHCN and ACIS, merges, pivots, and adds derived variables | `cleaned`, `analytics.observations` | Daily 23:45 ET | Yes |
| `wx_ghcn_validation_sync` | Pushes NOAA's revisions of loaded GHCN values (corrections, later QC flags) from `obs_ghcn_conv` to the downstream tables | `cleaned.obs_merged`, `cleaned.obs_pivot`, `analytics.observations` | Monthly, 1st at 02:00 ET | Yes |
| `wx_station_ingest` | Fetches, filters, and merges the EMSHR and GHCN station lists | `raw`, `cleaned` | On demand | Yes (and on success) |
| `wx_activate_station` | Activates a station, recreates the `stations` view, and backfills its observations | `raw`, `cleaned`, `analytics` | On demand (job parameters) | No |
| `wx_load_station_history` | Loads observations from a station's older IDs (has `dry_run`) | `raw.obs_ghcn` | On demand (job parameters) | No |
| `wx_places_ingest` | Loads the GeoNames gazetteer (places with 1,000+ people) and recreates the `places` view | `raw.geonames_raw`, `cleaned.places`, `analytics.places` | On demand only | Yes |
| `wx_app_schema` | Creates the `app` schema and tables (`IF NOT EXISTS`) | `app` | On demand, after a deploy | Yes |
| `wx_app_retention` | Rolls up daily usage, then deletes old turns (section 8) | `app` | Daily 03:00 ET | Yes |

**Identity.** Each target sets `run_as` in `databricks.yml`: prod jobs run as the prod jobs service principal, dev jobs as the dev one. A job can only write its own catalog because its identity has grants on no other, not just because of its `--catalog` argument.

**Permissions.** The owner has `IS_OWNER` (and `CAN_MANAGE` from `databricks.yml`) in both targets, the target's jobs service principal has `CAN_VIEW`, and the workspace `admins` group has `CAN_MANAGE`. Dev jobs are prefixed `[dev <user>]` by development mode.

**Dev.** The five scheduled jobs are paused in the dev target (`databricks.yml`), so dev only changes when a job is run by hand.

**The app never runs jobs.** The app service principals have no job permissions and no write access to `raw`, `cleaned`, or `analytics`.

## 6. Dev/prod isolation

Dev and testing environments (local Streamlit, the test harness, tests, the local MCP server) can't reach prod, even if a config names it. Prod is reached only by the deployed app (Streamlit Cloud) and the prod jobs. Both catalogs share one workspace; the boundary is identity and where credentials live, not config.

**THIS IS BY DESIGN:** ANY DEVEVELOPMENT TESTING CANNOT AND MUST NOT TOUCH ANY PRODUCTION ENVIRONMENTS, NOT EVEN READ FROM IT. TOTAL AIRGAPPED ISOLATION.

- **Identities.** Each environment has its own app and jobs service principals, granted on their own catalog only (section 3). Nothing used locally holds a prod grant.

- **Credentials.** The prod app service principal's secret and the prod Anthropic key exist only in Streamlit Cloud's secrets. Local `.env` holds only dev values. The jobs service principals have no secrets at all.

- **Prod is hidden, not just unreadable.** With `BROWSE` revoked from account users, the dev app service principal doesn't see `weather` in `SHOW CATALOGS`; any read fails with `PERMISSION_DENIED` or `INSUFFICIENT_PERMISSIONS`.

- **A clear refusal.** If `WX_CATALOG=weather` is set where prod isn't reachable, `wxcore.db.env_isolation_warning` turns the permission error into "Sorry, this client does not have production access to the weather observations database." (the Streamlit app, the MCP server, and the test harness use it). A permission error on dev keeps the generic message.

- **Code** never hard-codes a catalog. `wxcore.db.catalog()` reads `WX_CATALOG` on each call and raises an error if it isn't set, so a missing setting can't fall back to prod.

- **SQL files** in `sql/uc/` are written against `weather.analytics.*`. `scripts/deploy_sql.sh --env dev` rewrites them to `weather_dev`, and `--env prod` asks for confirmation before deploying.

- **Data.** Every dev table is stored in dev's own storage; none is a view over prod or a shallow clone of it (both read prod's data). To refresh dev from prod, the owner uses DEEP CLONE, never SHALLOW CLONE. `station_aliases`, the one table no job writes, is loaded into each catalog from the seed file `sql/uc/station_aliases.sql` (`scripts/deploy_sql.sh --env dev|prod station_aliases`), so dev and prod match without copying.

- **Jobs** get the catalog from the bundle target (`--catalog`) and run as that target's jobs service principal. Dev schedules are paused.

**Checking it.** A local script, `tests/check_airgap_live.py`, connects with the local credentials and must find: `.env` holding only dev settings, prod catalogs hidden, every prod read blocked, and dev working. It also warns about any active personal access token.

**By design, outside the boundary:** the owner's own login reaches prod (it owns the catalogs and deploys from the CLI). Dev and prod share one workspace; separation is by identity and grants, not workspace bindings.

## 7. Secrets

| Secret | Local `.env` (dev) | Streamlit Cloud (prod) |
|---|---|---|
| Databricks host, warehouse HTTP path | yes | yes |
| App service principal client ID and secret | dev app service principal | prod app service principal |
| Anthropic API key | dev key | prod key |
| `WX_CATALOG` | `weather_dev` | `weather` |

- `.env` is gitignored, so secrets never go in the repo.

- The app authenticates as a service principal with OAuth (client ID and secret), not with a personal access token. The secrets are scoped to all APIs, read/write; what a service principal can do is limited by its entitlement, warehouse permission, and grants.

- The Anthropic keys live in separate Anthropic workspaces (dev and prod), each with a monthly spend limit, so usage and cost are tracked per environment.

- The jobs service principals have no secrets.

- The deploy tooling (`databricks` CLI, `deploy_sql.sh`) authenticates as the owner through the CLI profile, not as a service principal.

## 8. User data and retention

The `app` schema is the only place the project stores data about people.

| Table | Holds | Kept |
|---|---|---|
| `app.users` | user_id, username, email, display name, units, home city, first/last seen | Named users only, indefinitely. Anonymous visitors aren't saved; their preferences last for the session |
| `app.chat_history` | Every turn: prompt, response, resolver JSON, model, status, error, station | 14 days |
| `app.usage_daily` | Turns, distinct users, and distinct sessions per UTC day and status; no content or ids | Indefinitely |

**How it runs.** The `wx_app_retention` job runs `src/wx/app/retention.py` daily at 03:00 ET in prod (paused in dev). Each run, in order:

1. Adds every completed day still in `chat_history` to `usage_daily`, skipping days already there.

2. Deletes turns older than 14 days.

3. Runs `REORG TABLE ... APPLY (PURGE)` and `VACUUM` on `chat_history`, so deleted rows are removed from storage, not just hidden. `VACUUM` removes files after Delta's 7-day retention window, so a deleted row is gone from storage (and from time travel) within about 7 days of being deleted.

The app only reads today's turns (the daily question cap), so the retention period doesn't change its behavior.

**Where questions go.** Every question in the app is sent to Anthropic's API (Claude) to be resolved and phrased. Nothing else leaves Databricks.

The MCP server never calls Claude and never sees the conversation, only its tool calls; results go to the user's MCP client and that client's model.

**Sign-in.** There's no login yet (`app/chat_store.py`): named users come from `WX_APP_USER` locally, and public visitors get an anonymous username.

## 9. Known gaps

These are open observations, not changes that have been made:

- **The MCP server has no prod identity yet.** When it's deployed, it gets its own prod service principal with `analytics` only (no `app` tables), and its secret only in the host's secret store.

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

To see only the grants that were issued directly, add `WHERE inherited_from = 'NONE'` around the query. Workspace permissions come from the CLI:

```bash
databricks warehouses list
databricks warehouses get-permissions <warehouse-id>
databricks jobs get-permissions <job-id>
```

Update section 3 whenever the explicit grants change.
