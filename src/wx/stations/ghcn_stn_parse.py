"""
Parse the raw, fixed-width GHCN-Daily station list lines into a typed table.

Fixed-width column positions follow the GHCN-Daily station list spec column layout:
    station_id 1-11
    latitude 13-20,
    longitude 22-30
    elevation 32-37
    state 39-40
    name 42-71
    gsn 73-75,
    hcn_crn 77-79
    wmo 81-85

This step always sets `selected=False` for every row, on every run since these
stations are assumed to not be selected for the app unless explicitly activated.
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

from wx.utils.config import (
    CATALOG,
    SCHEMA_CLEANED,
    STATION_LIST_RAW_TABLE,
    STATION_LIST_PARSED_TABLE,
)
from pyspark.sql import functions as F  # type: ignore
from wx.utils.spark_utils import get_spark


# Convert empty values to NULL
def null_if_blank(col):
    """Convert a blank (whitespace-only or empty) string column to NULL."""
    return F.when(F.trim(col) == "", None).otherwise(F.trim(col))


def parse_station_list(df):
    """Parse raw fixed-width station list lines into typed columns."""
    parsed = (
        df.withColumn("station_id", F.trim(F.substring("raw_line", 1, 11)))
        .withColumn("latitude", F.trim(F.substring("raw_line", 13, 8)).cast("double"))
        .withColumn("latitude", F.trim(F.substring("raw_line", 13, 8)).cast("double"))
        .withColumn("longitude", F.trim(F.substring("raw_line", 22, 9)).cast("double"))
        .withColumn("elevation", F.trim(F.substring("raw_line", 32, 6)).cast("double"))
        .withColumn("state", F.trim(F.substring("raw_line", 39, 2)))
        .withColumn("name", F.trim(F.substring("raw_line", 42, 30)))
        .withColumn("gsn", null_if_blank(F.trim(F.substring("raw_line", 73, 3))))
        .withColumn("hcn_crn", null_if_blank(F.trim(F.substring("raw_line", 77, 3))))
        .withColumn("wmo", null_if_blank(F.trim(F.substring("raw_line", 81, 5))))
        .withColumn("selected", F.lit(False))
        .select(
            "station_id",
            "latitude",
            "longitude",
            "elevation",
            "state",
            "name",
            "gsn",
            "hcn_crn",
            "wmo",
            "raw_line",
            "source_file",
            "ingested_at",
            "selected",
        )
    )
    return parsed


def main():
    spark = get_spark()

    raw_df = spark.table(STATION_LIST_RAW_TABLE)
    parsed_df = parse_station_list(raw_df)

    spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA_CLEANED}")
    parsed_df.write.format("delta").mode("overwrite").saveAsTable(
        STATION_LIST_PARSED_TABLE
    )

    stations = spark.table(STATION_LIST_PARSED_TABLE).count()
    print(f"Wrote {stations} stations to {STATION_LIST_PARSED_TABLE}")


if __name__ == "__main__":
    main()
