# Add derived temperature variables to obs_pivot, write to the analytics layer.

"""
Computes per (station_id, obs_date), from cleaned.obs_pivot into
analytics.observations:

    TAVG (Average Temperature)  = (TMAX + TMIN) / 2
    HDD  (Heating Degree Days)  = max(0, 65 - TAVG)
    CDD  (Cooling Degree Days)  = max(0, TAVG - 65)

Incremental, like pivot_obs.py: reprocesses obs_date >= the last GHCN date
in obs_pivot (from pivot_obs's task value in wx_obs_transform). --station-id
re-derives one station's whole history (station backfill).

HDD/CDD are NULL when TAVG is NULL, so missing days don't count as zero.
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

from pyspark.sql import DataFrame  # type: ignore
from pyspark.sql import functions as F  # type: ignore
from wx.utils.common import get_cutoff, upsert_dataframe
from wx.utils.config import (
    OBS_ANALYTICS_TABLE,
    OBS_PIVOT_TABLE,
)
from wx.utils.spark_utils import get_spark

# ---------------------------------------------------------------------------
# Incremental read
# ---------------------------------------------------------------------------


def read_new_pivot_rows(spark, only_station_id: str | None = None) -> DataFrame:
    """
    obs_pivot rows from ghcn_last_date on (all of them on the first run).
    only_station_id: that station's full history, no watermark.
    """
    pivot_df = spark.table(OBS_PIVOT_TABLE)

    if only_station_id:
        return pivot_df.filter(F.col("station_id") == F.lit(only_station_id))

    if not spark.catalog.tableExists(OBS_ANALYTICS_TABLE):
        return pivot_df

    # Same date pivot_obs used this run; scan only if it isn't available.
    ghcn_last_date = get_cutoff(upstream_task_key="pivot_obs")
    if ghcn_last_date is None:
        ghcn_last_date = (
            pivot_df.filter(F.col("source") == "GHCN")
            .agg(F.max("obs_date"))
            .collect()[0][0]
        )
    if ghcn_last_date is None:
        return pivot_df

    return pivot_df.filter(F.col("obs_date") >= F.lit(ghcn_last_date))


# ---------------------------------------------------------------------------
# Derived variables
# ---------------------------------------------------------------------------


def add_derived_vars(df: DataFrame) -> DataFrame:
    """
    TAVG = (TMAX + TMIN) / 2
    HDD = 65 - TAVG when TAVG < 65, else 0 (rounded to the nearest integer)
    CDD = TAVG - 65 when TAVG > 65, else 0 (rounded to the nearest integer)
    HDD and CDD are NULL when TAVG is NULL.
    """
    df = df.withColumn("TAVG", (F.col("TMAX") + F.col("TMIN")) / 2.0)
    df = df.withColumn(
        "HDD",
        F.when(
            F.col("TAVG").isNotNull(),
            F.round(
                F.when(F.col("TAVG") < 65, F.lit(65) - F.col("TAVG")).otherwise(F.lit(0.0))
            ).cast("int"),
        ),
    )
    df = df.withColumn(
        "CDD",
        F.when(
            F.col("TAVG").isNotNull(),
            F.round(
                F.when(F.col("TAVG") > 65, F.col("TAVG") - F.lit(65)).otherwise(F.lit(0.0))
            ).cast("int"),
        ),
    )
    return df


def main():
    parser = argparse.ArgumentParser(
        description="Add derived vars to obs_pivot, write to the analytics layer."
    )
    parser.add_argument(
        "--station-id",
        default=None,
        help=(
            "Re-derive only this station's full obs_pivot history, ignoring the "
            "date watermark. Use to backfill a newly activated station."
        ),
    )
    args, _ = parser.parse_known_args()

    spark = get_spark()

    new_pivot_df = read_new_pivot_rows(spark, only_station_id=args.station_id or None)
    count = new_pivot_df.count()
    print(f"New/changed obs_pivot records to process: {count:,}")

    if count == 0:
        print("Nothing new to process.")
        return

    derived_df = add_derived_vars(new_pivot_df)

    upsert_dataframe(
        spark, derived_df, OBS_ANALYTICS_TABLE, merge_keys=("station_id", "obs_date")
    )

    print(f"Merged into {OBS_ANALYTICS_TABLE}")


if __name__ == "__main__":
    main()
