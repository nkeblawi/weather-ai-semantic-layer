"""
Applies the `app` schema's data retention policy (docs/governance.md):

1. Rolls up every completed UTC day in `app.chat_history` into
   `app.usage_daily` (turns, distinct users, distinct sessions per status).
   Days already recorded are left as they are, so a re-run adds nothing, and
   the first run backfills every day still in chat_history.
2. Deletes `app.chat_history` turns older than CHAT_RETENTION_DAYS.
3. Purges the deleted rows from the table's files (REORG ... APPLY (PURGE),
   which rewrites files that only mark rows deleted) and runs VACUUM, which
   removes files older than Delta's retention window (7 days by default).
   So deleted rows leave queries right away and storage within ~7 days.

Runs as the single task in the wx_app_retention job: daily in prod, paused
in dev (databricks.yml). Step 1 runs first in the same run, so no day is
purged before it has been counted.

--catalog (passed once per bundle target: dev -> weather_dev, prod ->
weather) is turned into WX_CATALOG by bootstrap(), so the table names below
resolve to the right catalog for the target that deployed this job.
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

from wx.utils.config import CHAT_HISTORY_TABLE, USAGE_DAILY_TABLE  # noqa: E402
from wx.utils.spark_utils import get_spark  # noqa: E402

CHAT_RETENTION_DAYS = 14


def _affected(df):
    """num_affected_rows / num_inserted_rows from a DML result, as an int."""
    row = df.collect()[0].asDict()
    return row.get("num_affected_rows", row.get("num_inserted_rows", 0))


def main():
    spark = get_spark()

    # 1) roll up completed days before anything is deleted
    inserted = _affected(spark.sql(f"""
        MERGE INTO {USAGE_DAILY_TABLE} AS t
        USING (
          SELECT to_date(created_at)        AS usage_date,
                 coalesce(status, 'unknown') AS status,
                 count(*)                   AS turns,
                 count(DISTINCT user_id)    AS users,
                 count(DISTINCT session_id) AS sessions
          FROM {CHAT_HISTORY_TABLE}
          WHERE created_at < current_date()
          GROUP BY 1, 2
        ) AS s
        ON t.usage_date = s.usage_date AND t.status = s.status
        WHEN NOT MATCHED THEN INSERT (usage_date, status, turns, users, sessions)
          VALUES (s.usage_date, s.status, s.turns, s.users, s.sessions)
        """))
    print(f"{USAGE_DAILY_TABLE}: {inserted} day/status rows added")

    # 2) old turns
    deleted = _affected(spark.sql(f"""
        DELETE FROM {CHAT_HISTORY_TABLE}
        WHERE created_at < current_timestamp() - INTERVAL {CHAT_RETENTION_DAYS} DAYS
        """))
    print(f"{CHAT_HISTORY_TABLE}: {deleted} turns older than {CHAT_RETENTION_DAYS} days deleted")

    # 3) remove deleted rows from storage
    spark.sql(f"REORG TABLE {CHAT_HISTORY_TABLE} APPLY (PURGE)")
    spark.sql(f"VACUUM {CHAT_HISTORY_TABLE}")
    print(f"{CHAT_HISTORY_TABLE}: purged and vacuumed")


if __name__ == "__main__":
    main()
