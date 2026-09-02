"""
Creates the `<catalog>.app` schema and its two tables that persist the
weather-query application's chat history so it can support multiple users:

- `app.users`         -- one row per user (identity + first/last seen)
- `app.chat_history`  -- one row per prompt/answer turn, linked to a user by
                         user_id and grouped into conversations by session_id

This is the src/wx counterpart of functions/sql for the `app` schema:
pipeline DDL run as a Databricks Job "Python script" task and deployed via
the asset bundle. It runs as the single task in the wx_app_schema job; on
demand, right after `databricks bundle deploy -t <target>`. No schedule.

The `app` schema holds application state, separate from weather data.

--catalog (passed once per bundle target: dev -> weather_dev, prod ->
weather) is turned into WX_CATALOG by bootstrap(), so USERS_TABLE /
CHAT_HISTORY_TABLE below resolve to the right catalog for the target that
deployed this job.

All statements are idempotent (CREATE ... IF NOT EXISTS), so re-running is
safe, but that also means column/constraint changes to an existing table
are NOT picked up on a re-run. Anytime the schema evolves, add explicit
ALTER TABLE statements here so a deploy + run applies them.

NOTE on constraints: Unity Catalog PRIMARY KEY / FOREIGN KEY are
informational (NOT ENFORCED). They document the model and feed
lineage/BI tools; the writing app must still guarantee turn_id/user_id
uniqueness and referential integrity itself.
"""

import os
import sys
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--workspace-root", default=os.environ.get("WX_WORKSPACE_ROOT"))
args, _ = parser.parse_known_args()
if not args.workspace_root:
    parser.error(
        "--workspace-root is required (or set WX_WORKSPACE_ROOT for manual/notebook testing)"
    )
sys.path.append(args.workspace_root)

from wx.utils.bootstrap import bootstrap  # noqa: E402

bootstrap()

from wx.utils.config import (  # noqa: E402
    CATALOG,
    SCHEMA_APP,
    USERS_TABLE,
    CHAT_HISTORY_TABLE,
)
from wx.utils.spark_utils import get_spark  # noqa: E402


def main():
    spark = get_spark()

    spark.sql(f"""
        CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA_APP}
        COMMENT 'User accounts and chat history for the weather-query application.'
        """)
    print(f"Ensured schema {CATALOG}.{SCHEMA_APP}")

    # --- APP.USERS ---
    # One row per user
    # user_id is an opaque surrogate key. Until a real auth provider is
    # wired in, the app derives it deterministically (uuid5) from `username`
    # an email or handle taken from an env var / prompt, so rows stay
    # stable when real auth is worked in and just starts populating `email`.
    spark.sql(f"""
        CREATE TABLE IF NOT EXISTS {USERS_TABLE} (
          user_id       STRING    NOT NULL
                        COMMENT 'Opaque surrogate key for the user. Stable across a later switch to real auth.',
          username      STRING    NOT NULL
                        COMMENT 'Local-stub identifier (email or handle) from an env var / prompt until real auth exists.',
          email         STRING
                        COMMENT 'Email address; null until an auth provider supplies it.',
          display_name  STRING
                        COMMENT 'Optional friendly name for display.',
          created_at    TIMESTAMP NOT NULL DEFAULT current_timestamp()
                        COMMENT 'When this user row was first created (UTC).',
          last_seen_at  TIMESTAMP
                        COMMENT 'Set by the app at the start of each new session (UTC).',
          is_active     BOOLEAN   NOT NULL DEFAULT true
                        COMMENT 'Soft-disable flag; false hides the user without deleting history.',
          temp_unit     STRING    NOT NULL DEFAULT 'F'
                        COMMENT 'One of F (Fahrenheit) or C (Celsius).',
          home_location STRING
                        COMMENT 'User''s home location; null until set by the app.',
          CONSTRAINT users_pk PRIMARY KEY (user_id)
        )
        USING DELTA
        COMMENT 'One row per weather-query application user.'
        TBLPROPERTIES ('delta.feature.allowColumnDefaults' = 'supported')
        """)
    print(f"Ensured table {USERS_TABLE}")

    # --- APP.CHAT_HISTORY ---
    # One row per prompt/answer turn
    spark.sql(f"""
        CREATE TABLE IF NOT EXISTS {CHAT_HISTORY_TABLE} (
          turn_id       STRING    NOT NULL
                        COMMENT 'UUID for this single prompt/answer turn.',
          user_id       STRING    NOT NULL
                        COMMENT 'User who sent the prompt; references app.users.user_id.',
          session_id    STRING    NOT NULL
                        COMMENT 'UUID grouping turns from one conversation / app run.',
          turn_index    INT
                        COMMENT '0-based position of this turn within its session.',
          created_at    TIMESTAMP NOT NULL DEFAULT current_timestamp()
                        COMMENT 'When the turn was recorded (UTC) -- the chat date/time.',
          prompt        STRING    NOT NULL
                        COMMENT 'The user''s raw natural-language question.',
          response      STRING
                        COMMENT 'Final natural-language answer; null on clarification or error.',
          status        STRING
                        COMMENT 'resolved | needs_clarification | error.',
          resolved_json STRING
                        COMMENT 'Resolver JSON output for this turn (audit / debug).',
          model         STRING
                        COMMENT 'Model id used for the turn, e.g. claude-haiku-4-5-20251001.',
          error         STRING
                        COMMENT 'Error detail when status = error.',
          station_id    STRING
                        COMMENT 'GHCN station this turn resolved to; null if unresolved. Join to {CATALOG}.analytics.stations for its name/city.',
          CONSTRAINT chat_history_pk PRIMARY KEY (turn_id),
          CONSTRAINT chat_history_user_fk FOREIGN KEY (user_id) REFERENCES {USERS_TABLE}
        )
        USING DELTA
        COMMENT 'One row per prompt/answer turn, linked to app.users by user_id.'
        TBLPROPERTIES ('delta.feature.allowColumnDefaults' = 'supported')
        """)
    print(f"Ensured table {CHAT_HISTORY_TABLE}")


if __name__ == "__main__":
    main()
