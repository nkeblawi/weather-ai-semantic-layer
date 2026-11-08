# Apply unit conversions to GHCN weather data (raw units -> transformed units).

"""
Converts raw GHCN values to app units and applies the QC rule.

Variable   Raw unit          Transformed unit   Formula
PRCP       tenths of mm      inches             round(tenths_mm * 0.00393701, 2)
SNOW       mm                inches             round(mm * 0.0393701, 1)
SNWD       mm                inches             round(mm * 0.0393701, 1)
TMAX       tenths of deg C   deg F              round((tenths_C * 0.18) + 32, 0)
TMIN       tenths of deg C   deg F              round((tenths_C * 0.18) + 32, 0)
AWND       tenths of m/s     mph                round(tenths_ms * 0.223694, 0)

QC: a value with a QFLAG (failed a NOAA check) becomes NULL, keeping the row
and flag. cleaned.qc_allowlist exempts values verified as real (e.g. SFO's
106F record); it's seeded by the "Create AllowList Table" saved query.

Incremental: reads only raw rows ingested after obs_ghcn_conv's max
ingested_at, and upserts on (station_id, obs_date, variable).
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
from wx.utils.common import upsert_dataframe
from wx.utils.config import OBS_GHCN_TABLE, OBS_GHCN_CONV_TABLE, QC_ALLOWLIST_TABLE
from wx.utils.spark_utils import get_spark

# ---------------------------------------------------------------------------
# QC filtering
# ---------------------------------------------------------------------------


def apply_qc_filter(spark, df: DataFrame) -> DataFrame:
    """NULLs value wherever qflag is set, except allowlisted keys; rows and flags stay."""
    allowlist = (
        spark.table(QC_ALLOWLIST_TABLE)
        .select("station_id", "obs_date", "variable")
        .withColumn("_allowlisted", F.lit(True))
    )

    return (
        df.join(F.broadcast(allowlist), on=["station_id", "obs_date", "variable"], how="left")
        .withColumn(
            "value",
            F.when(
                F.col("qflag").isNotNull() & F.col("_allowlisted").isNull(),
                F.lit(None).cast("double"),
            ).otherwise(F.col("value")),
        )
        .drop("_allowlisted")
    )


# ---------------------------------------------------------------------------
# Unit conversions
# ---------------------------------------------------------------------------


def apply_ghcn_unit_conversions(df: DataFrame) -> DataFrame:
    """Converts value to app units by variable; unknown variables pass through."""
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
    """Raw rows ingested after obs_ghcn_conv's max ingested_at (all of them on the first run)."""
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
    converted_df = apply_ghcn_unit_conversions(apply_qc_filter(spark, new_raw_df))

    count = converted_df.count()
    print(f"New/changed records to convert: {count:,}")

    if count == 0:
        print("Nothing new to convert.")
        return

    upsert_dataframe(spark, converted_df, OBS_GHCN_CONV_TABLE)

    print(f"Converted data merged into {OBS_GHCN_CONV_TABLE}")


if __name__ == "__main__":
    main()
