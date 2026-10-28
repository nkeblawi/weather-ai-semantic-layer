# Deploying and Operations

## Environments

The Databricks Asset Bundle (`databricks.yml`, `resources/`) deploys to two
targets, each pointing at a separate Unity Catalog catalog so pipeline
changes can be tested without touching production data:

- `dev` → `weather_dev`
- `prod` → `weather`

## Deploying changes

- **SQL functions:** `scripts/deploy_sql.sh --env dev|prod [function ...]`
  (add `--dry-run` to preview). Deploy to dev first, and deploy SQL before
  the app code that depends on it. `CREATE OR REPLACE FUNCTION` drops the
  function's own grants, but the app keeps access through `EXECUTE` on the
  `analytics` schema (see [governance.md](governance.md)).
- **Pipeline code (`src/wx/`):** `databricks bundle deploy -t dev|prod`
  deploys everything. To update only a few files, upload each one with
  `databricks workspace import ... --overwrite` to the bundle's `files/src/`
  path, shared modules (`wx/utils/`) before the scripts that import them, and
  not while a job that uses them is running.
- **App:** Streamlit Community Cloud redeploys on every push to `main`, but
  it doesn't reliably reload changed Python modules (`app/guardrails.py`,
  `app/pipeline.py`, `app/chat_session.py`, and everything in `wxcore/`);
  the running app keeps the old
  versions. After pushing changes to any of them, **reboot the app** from
  the Community Cloud dashboard. Skill files (`skills/`) are read on every
  question, so they take effect without a reboot. Usage limits set in the
  app's secrets (`WX_*_LIMIT`) are read at startup, so changing one also
  needs a reboot.

## Backfilling observations

`scripts/backfill.sh --date YYYY-MM-DD --env dev|prod` re-runs the
observation pipeline from a date through yesterday, each job waiting for
the previous one:

1. `wx_obs_ingest_ghcn`: one run, sized to cover the gap (a full reload if
   the gap is over 12 months).
2. `wx_obs_ingest_acis`: one run per date for the last 2 days only. GHCN
   wins in the merge wherever it has data, so ACIS only matters for days
   GHCN hasn't published yet.
3. `wx_obs_transform`: one run per selected station, re-pivoting that
   station's full history.

Both flags are required (there is no default environment, so prod is never
hit by accident), and `--env prod` prints the plan and asks for
confirmation before any job is triggered.
