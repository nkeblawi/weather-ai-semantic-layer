# Add derived temperature variables to obs_pivot, write to the analytics layer.

"""
Converted from the OBS_05_DerivedVars notebook. Computes, per (station_id,
obs_date):

    TAVG (Average Temperature)  = (TMAX + TMIN) / 2
    HDD  (Heating Degree Days)  = max(0, 65 - TAVG)
    CDD  (Cooling Degree Days)  = max(0, TAVG - 65)

Source: weather.cleaned.obs_pivot
Target: weather.analytics.observations

Incremental: same design as pivot_obs.py, one layer further down the
pipeline. obs_pivot's own `source` column can flip from ACIS to GHCN in
place (pivot_obs.py upserts on (station_id, obs_date), same key, no
obs_date change), so this watermarks the same way: ghcn_last_date =
MAX(obs_date) where source = 'GHCN' in obs_pivot, recomputed fresh every
run, reprocessing obs_date >= ghcn_last_date. No stored watermark.

Note: matching the original notebook's CASE WHEN behavior exactly, HDD/CDD
resolve to 0 (not NULL) when TAVG is NULL, since a SQL CASE (and
PySpark's equivalent F.when) falls through to ELSE/otherwise when the
condition itself is unknown.
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

from wx.utils.bootstrap import bootstrap  # noqa: E402
bootstrap()

from pyspark.sql import DataFrame  # type: ignore
from pyspark.sql import functions as F  # type: ignore
from wx.utils.common import upsert_dataframe
from wx.utils.config import (
    OBS_ANALYTICS_TABLE,
    OBS_PIVOT_TABLE,
)
from wx.utils.spark_utils import get_spark

# ---------------------------------------------------------------------------
# Incremental read
# ---------------------------------------------------------------------------


def read_new_pivot_rows(spark) -> DataFrame:
    """
    Read obs_pivot rows with obs_date >= ghcn_last_date (the most recent
    obs_date with source = 'GHCN' in obs_pivot). Reads the full table on
    first run, when the analytics table doesn't exist yet.
    """
    pivot_df = spark.table(OBS_PIVOT_TABLE)

    if not spark.catalog.tableExists(OBS_ANALYTICS_TABLE):
        return pivot_df

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
    """
    df = df.withColumn("TAVG", (F.col("TMAX") + F.col("TMIN")) / 2.0)
    df = df.withColumn(
        "HDD",
        F.round(
            F.when(F.col("TAVG") < 65, F.lit(65) - F.col("TAVG")).otherwise(F.lit(0.0))
        ).cast("int"),
    )
    df = df.withColumn(
        "CDD",
        F.round(
            F.when(F.col("TAVG") > 65, F.col("TAVG") - F.lit(65)).otherwise(F.lit(0.0))
        ).cast("int"),
    )
    return df


def main():
    spark = get_spark()

    new_pivot_df = read_new_pivot_rows(spark)
    count = new_pivot_df.count()
    print(f"New/changed obs_pivot records to process: {count:,}")

    if count == 0:
        print("Nothing new to process.")
        return

    derived_df = add_derived_vars(new_pivot_df)
    print(f"Computed derived variables for {derived_df.count():,} rows")

    upsert_dataframe(
        spark, derived_df, OBS_ANALYTICS_TABLE, merge_keys=("station_id", "obs_date")
    )

    print(f"Merged into {OBS_ANALYTICS_TABLE}")


if __name__ == "__main__":
    main()
