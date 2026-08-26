# Pivot obs_merged (long: one row per station/date/variable) into obs_pivot
# (wide: one row per station/date, variables as columns).

"""
Reshapes weather.cleaned.obs_merged into weather.cleaned.obs_pivot: one row
per (station_id, obs_date), with each meteorological variable (TMAX, TMIN,
PRCP, SNOW, SNWD, AWND) as its own column. Quality flags (mflag/qflag/sflag)
and ingested_at are dropped -- not needed downstream of this table.

Source column: normally uniform across all variables for a given station/
date (confirmed in practice), but the per-row merge priority logic in
merge_obs.py technically allows a date to have variables from mixed
sources. When that happens, GHCN wins the tiebreak (GHCN is the more
authoritative source once its data catches up).

Incremental: watermarks on how far GHCN has progressed, not on obs_pivot's
own state. ghcn_last_date = MAX(obs_date) where source = 'GHCN' in
obs_merged (same concept merge_obs.py already uses to decide ACIS-vs-GHCN
priority), recomputed fresh from obs_merged every run -- no stored
watermark, no side table. Reprocesses obs_date >= ghcn_last_date each
run, which covers both:
  - brand-new dates (ACIS running ahead of GHCN), and
  - the trailing dates GHCN might still catch up on and replace an
    existing ACIS row for -- merge_obs.py updates that row in place
    without changing its obs_date, so a plain "obs_date > previous max"
    watermark would silently miss it. Anchoring to ghcn_last_date instead
    means we keep re-pivoting exactly the window where that can still
    happen, until GHCN moves past it.
Dates older than ghcn_last_date are settled (GHCN has already progressed
past them) and are not reprocessed.
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
from wx.utils.config import OBS_MERGED_TABLE, OBS_PIVOT_TABLE
from wx.utils.spark_utils import get_spark

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

VARIABLES = ["AWND", "PRCP", "SNOW", "SNWD", "TMAX", "TMIN"]

# ---------------------------------------------------------------------------
# Incremental read
# ---------------------------------------------------------------------------


def read_new_merged_rows(spark) -> DataFrame:
    """
    Read obs_merged rows with obs_date >= ghcn_last_date (the most recent
    obs_date GHCN has non-null data for). Reads the full table on first
    run, when obs_pivot doesn't exist yet.
    """
    merged_df = spark.table(OBS_MERGED_TABLE)

    if not spark.catalog.tableExists(OBS_PIVOT_TABLE):
        return merged_df

    ghcn_last_date = (
        merged_df.filter((F.col("source") == "GHCN") & F.col("value").isNotNull())
        .agg(F.max("obs_date"))
        .collect()[0][0]
    )
    if ghcn_last_date is None:
        return merged_df

    return merged_df.filter(F.col("obs_date") >= F.lit(ghcn_last_date))


# ---------------------------------------------------------------------------
# Pivot
# ---------------------------------------------------------------------------


def pivot_obs(df: DataFrame) -> DataFrame:
    """
    One row per (station_id, obs_date): variables become columns, source
    is the shared source across that date's variables, or 'GHCN' if the
    date turns out to have mixed sources.
    """
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


def main():
    spark = get_spark()

    new_merged_df = read_new_merged_rows(spark)

    count = new_merged_df.count()
    print(f"New/changed obs_merged records to pivot: {count:,}")

    if count == 0:
        print("Nothing new to pivot.")
        return

    pivot_df = pivot_obs(new_merged_df)
    print(f"Pivoted to {pivot_df.count():,} station/date rows")

    upsert_dataframe(
        spark, pivot_df, OBS_PIVOT_TABLE, merge_keys=("station_id", "obs_date")
    )

    print(f"Merged into {OBS_PIVOT_TABLE}")


if __name__ == "__main__":
    main()
