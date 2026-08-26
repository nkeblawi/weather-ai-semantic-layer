# Map ACIS variable names to standardized GHCN variable names.

"""
Converted from the OBS_02_Col_Map notebook. ACIS data is already in the
target units (inches, degrees F), so no unit conversion happens here --
just remapping ACIS's native element codes to GHCN-D equivalents:

    pcpn -> PRCP
    snow -> SNOW
    snwd -> SNWD
    maxt -> TMAX
    mint -> TMIN

Incremental: only raw rows ingested after obs_acis_conv's current max
ingested_at are read and transformed each run, then merged/upserted on
(station_id, obs_date, variable). Avoids re-reading and rewriting all of
obs_acis every run as station count and history grow -- cost scales with
the new/changed rows since last run, not total accumulated history.
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
from wx.utils.config import OBS_ACIS_TABLE, OBS_ACIS_CONV_TABLE
from wx.utils.spark_utils import get_spark

# ---------------------------------------------------------------------------
# Variable mapping
# ---------------------------------------------------------------------------


def map_acis_variable_names(df: DataFrame) -> DataFrame:
    """
    Map ACIS variable names (in the 'variable' column) to standardized
    GHCN variable names. Unknown variables pass through unchanged.
    """
    return df.withColumn(
        "variable",
        F.when(F.col("variable") == "pcpn", "PRCP")
        .when(F.col("variable") == "snow", "SNOW")
        .when(F.col("variable") == "snwd", "SNWD")
        .when(F.col("variable") == "maxt", "TMAX")
        .when(F.col("variable") == "mint", "TMIN")
        .otherwise(F.col("variable")),
    )


# ---------------------------------------------------------------------------
# Incremental read
# ---------------------------------------------------------------------------


def read_new_raw_rows(spark) -> DataFrame:
    """
    Read raw obs_acis rows ingested after obs_acis_conv's current max
    ingested_at. Reads the full raw table on first run, when the conv
    table doesn't exist yet.
    """
    raw_df = spark.table(OBS_ACIS_TABLE)

    if not spark.catalog.tableExists(OBS_ACIS_CONV_TABLE):
        return raw_df

    watermark = (
        spark.table(OBS_ACIS_CONV_TABLE).agg(F.max("ingested_at")).collect()[0][0]
    )
    if watermark is None:
        return raw_df

    return raw_df.filter(F.col("ingested_at") > F.lit(watermark))


def main():
    spark = get_spark()

    new_raw_df = read_new_raw_rows(spark)
    mapped_df = map_acis_variable_names(new_raw_df)

    count = mapped_df.count()
    print(f"New/changed records to map: {count:,}")

    if count == 0:
        print("Nothing new to map.")
        return

    upsert_dataframe(spark, mapped_df, OBS_ACIS_CONV_TABLE)

    print(f"Mapped data merged into {OBS_ACIS_CONV_TABLE}")


if __name__ == "__main__":
    main()
