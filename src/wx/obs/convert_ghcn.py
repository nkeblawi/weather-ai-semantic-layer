# Apply unit conversions to GHCN weather data (raw units -> transformed units).

"""
Converted from the OBS_02_Unit_Conversions notebook.

Variable   Raw unit          Transformed unit   Formula
PRCP       tenths of mm      inches             round(tenths_mm * 0.00393701, 2)
SNOW       mm                inches             round(mm * 0.0393701, 1)
SNWD       mm                inches             round(mm * 0.0393701, 1)
TMAX       tenths of deg C   deg F              round((tenths_C * 0.18) + 32, 0)
TMIN       tenths of deg C   deg F              round((tenths_C * 0.18) + 32, 0)
AWND       tenths of m/s     mph                round(tenths_ms * 0.223694, 0)

Incremental: only raw rows ingested after obs_ghcn_conv's current max
ingested_at are read and transformed each run, then merged/upserted on
(station_id, obs_date, variable). Avoids re-reading and rewriting all of
obs_ghcn every run as station count and history grow -- cost scales with
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
from wx.utils.config import OBS_GHCN_TABLE, OBS_GHCN_CONV_TABLE
from wx.utils.spark_utils import get_spark

# ---------------------------------------------------------------------------
# Unit conversions
# ---------------------------------------------------------------------------


def apply_ghcn_unit_conversions(df: DataFrame) -> DataFrame:
    """
    Apply GHCN unit conversions from raw to transformed units on the
    'value' column, based on the 'variable' column. Unknown variables
    pass through unchanged.
    """
    return df.withColumn(
        "value",
        F.when(F.col("variable") == "PRCP", F.round(F.col("value") * 0.00393701, 2))
        .when(F.col("variable") == "SNOW", F.round(F.col("value") * 0.0393701, 1))
        .when(F.col("variable") == "SNWD", F.round(F.col("value") * 0.0393701, 1))
        .when(F.col("variable") == "TMAX", F.round((F.col("value") * 0.18) + 32, 0))
        .when(F.col("variable") == "TMIN", F.round((F.col("value") * 0.18) + 32, 0))
        .when(F.col("variable") == "AWND", F.round(F.col("value") * 0.223694, 0))
        .otherwise(F.col("value")),
    )


# ---------------------------------------------------------------------------
# Incremental read
# ---------------------------------------------------------------------------


def read_new_raw_rows(spark) -> DataFrame:
    """
    Read raw obs_ghcn rows ingested after obs_ghcn_conv's current max
    ingested_at. Reads the full raw table on first run, when the conv
    table doesn't exist yet.
    """
    raw_df = spark.table(OBS_GHCN_TABLE)

    if not spark.catalog.tableExists(OBS_GHCN_CONV_TABLE):
        return raw_df

    watermark = (
        spark.table(OBS_GHCN_CONV_TABLE).agg(F.max("ingested_at")).collect()[0][0]
    )
    if watermark is None:
        return raw_df

    return raw_df.filter(F.col("ingested_at") > F.lit(watermark))


def main():
    spark = get_spark()

    new_raw_df = read_new_raw_rows(spark)
    converted_df = apply_ghcn_unit_conversions(new_raw_df)

    count = converted_df.count()
    print(f"New/changed records to convert: {count:,}")

    if count == 0:
        print("Nothing new to convert.")
        return

    upsert_dataframe(spark, converted_df, OBS_GHCN_CONV_TABLE)

    print(f"Converted data merged into {OBS_GHCN_CONV_TABLE}")


if __name__ == "__main__":
    main()
