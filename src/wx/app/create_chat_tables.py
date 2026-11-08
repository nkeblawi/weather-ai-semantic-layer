"""
Creates the app schema's tables (run by hand with the wx_app_schema job):

- app.users: one row per user
- app.chat_history: one row per turn, by user_id and session_id
- app.usage_daily: turn counts per day and status, kept after retention.py
  purges the turns

CREATE ... IF NOT EXISTS, so reruns are safe but don't change an existing
table: add ALTER TABLE statements for schema changes. Unity Catalog keys
are informational only; the app keeps ids unique itself.
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

from wx.utils.bootstrap import bootstrap

bootstrap()

from wx.utils.config import (
    CATALOG,
    SCHEMA_APP,
    USERS_TABLE,
    CHAT_HISTORY_TABLE,
    USAGE_DAILY_TABLE,
)
from wx.utils.spark_utils import get_spark

# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------


def main():
    spark = get_spark()

    spark.sql(f"""
        CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA_APP}
        COMMENT 'User accounts and chat history for the weather-query application.'
        """)
    print(f"Ensured schema {CATALOG}.{SCHEMA_APP}")

    # --- APP.USERS ---
    # One row per user
    # user_id: uuid5 of the username (no login yet)
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

    # --- APP.USAGE_DAILY ---
    # One row per UTC day and turn status, counts only, written by retention.py before purging
    spark.sql(f"""
        CREATE TABLE IF NOT EXISTS {USAGE_DAILY_TABLE} (
          usage_date    DATE      NOT NULL
                        COMMENT 'UTC day the turns were recorded.',
          status        STRING    NOT NULL
                        COMMENT 'resolved | needs_clarification | error (unknown if the turn had none).',
          turns         BIGINT    NOT NULL
                        COMMENT 'Turns recorded that day with this status.',
          users         BIGINT    NOT NULL
                        COMMENT 'Distinct user_ids among those turns.',
          sessions      BIGINT    NOT NULL
                        COMMENT 'Distinct session_ids among those turns.',
          recorded_at   TIMESTAMP NOT NULL DEFAULT current_timestamp()
                        COMMENT 'When retention.py wrote this row (UTC).',
          CONSTRAINT usage_daily_pk PRIMARY KEY (usage_date, status)
        )
        USING DELTA
        COMMENT 'Daily turn counts from app.chat_history, kept after the turns are purged.'
        TBLPROPERTIES ('delta.feature.allowColumnDefaults' = 'supported')
        """)
    print(f"Ensured table {USAGE_DAILY_TABLE}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
