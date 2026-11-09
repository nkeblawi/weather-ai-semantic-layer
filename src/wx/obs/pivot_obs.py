# Pivot obs_merged (long: one row per station/date/variable) into obs_pivot
# (wide: one row per station/date, variables as columns).

"""
Reshapes cleaned.obs_merged into cleaned.obs_pivot: one row per
(station_id, obs_date), variables as columns, flags dropped. A date with
mixed sources is labeled GHCN.

Incremental: reprocesses obs_date >= the last GHCN date in obs_merged (from
merge_obs's task value in wx_obs_transform). That covers new ACIS dates and
the recent ones where GHCN can still replace ACIS in place, which an
"after the previous max" watermark would miss. Older dates are settled; a
late value needs a backfill. --station-id re-pivots one station's whole
history (station backfill).
"""

import os
import sys
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--workspace-root", default=os.environ.get("WX_WORKSPACE_ROOT"))
args, _ = parser.parse_known_args()
if not args.workspace_root:
    parser.error("--workspace-root is required (or set WX_WORKSPACE_ROOT for manual/notebook testing)")
sys.path.append(args.workspace_root)

from wx.utils.bootstrap import bootstrap
bootstrap()


from pyspark.sql import DataFrame
from pyspark.sql import functions as F
from wx.utils.common import get_cutoff, set_cutoff, upsert_dataframe
from wx.utils.config import OBS_MERGED_TABLE, OBS_PIVOT_TABLE
from wx.utils.spark_utils import get_spark

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VARIABLES = ["AWND", "PRCP", "SNOW", "SNWD", "TMAX", "TMIN"]


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

# --- Incremental read ---


def _read_new_merged_rows(spark, only_station_id: str | None = None) -> DataFrame:
    """
    obs_merged rows from ghcn_last_date on (all of them on the first run).
    only_station_id: that station's full history, no watermark.
    """
    merged_df = spark.table(OBS_MERGED_TABLE)

    if only_station_id:
        return merged_df.filter(F.col("station_id") == F.lit(only_station_id))

    if not spark.catalog.tableExists(OBS_PIVOT_TABLE):
        return merged_df

    # Same date merge_obs computed this run; scan only if it isn't available.
    ghcn_last_date = get_cutoff(upstream_task_key="merge_obs")
    if ghcn_last_date is None:
        ghcn_last_date = (
            merged_df.filter((F.col("source") == "GHCN") & F.col("value").isNotNull())
            .agg(F.max("obs_date"))
            .collect()[0][0]
        )
    set_cutoff(ghcn_last_date)
    if ghcn_last_date is None:
        return merged_df

    return merged_df.filter(F.col("obs_date") >= F.lit(ghcn_last_date))


# --- Pivot ---


def _pivot_obs(df: DataFrame) -> DataFrame:
    """One row per (station_id, obs_date), variables as columns; mixed sources -> GHCN."""
    pivoted = (
        df.groupBy("station_id", "obs_date")
        .pivot("variable", VARIABLES)
        .agg(F.first("value"))
    )

    source_df = (
        df.groupBy("station_id", "obs_date")
        .agg(
            F.countDistinct("source").alias("_distinct_sources"),
            F.min("source").alias("_single_source"),
        )
        .withColumn(
            "source",
            F.when(F.col("_distinct_sources") > 1, F.lit("GHCN")).otherwise(
                F.col("_single_source")
            ),
        )
        .select("station_id", "obs_date", "source")
    )

    return pivoted.join(source_df, on=["station_id", "obs_date"], how="inner").select(
        "station_id", "obs_date", *VARIABLES, "source"
    )


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Pivot obs_merged into obs_pivot.")
    parser.add_argument(
        "--station-id",
        default=None,
        help=(
            "Re-pivot only this station's full obs_merged history, ignoring the "
            "date watermark. Use to backfill a newly activated station."
        ),
    )
    args, _ = parser.parse_known_args()

    spark = get_spark()

    new_merged_df = _read_new_merged_rows(spark, only_station_id=args.station_id or None)

    count = new_merged_df.count()
    print(f"New/changed obs_merged records to pivot: {count:,}")

    if count == 0:
        print("Nothing new to pivot.")
        return

    pivot_df = _pivot_obs(new_merged_df)

    upsert_dataframe(
        spark, pivot_df, OBS_PIVOT_TABLE, merge_keys=("station_id", "obs_date")
    )

    print(f"Merged into {OBS_PIVOT_TABLE}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
