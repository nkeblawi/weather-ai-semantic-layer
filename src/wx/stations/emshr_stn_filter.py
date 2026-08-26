"""
Filter the EMSHR station list to only WMO-identified stations.
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

from wx.utils.config import STATION_EMSHR_RAW_TABLE, STATION_EMSHR_FILTERED_TABLE
from pyspark.sql import functions as F  # type: ignore
from pyspark.sql.window import Window  # type: ignore
from wx.utils.http_utils import get_session
from wx.utils.spark_utils import get_spark


def filter_null_ghcn_rows(raw_df):
    return raw_df.filter(F.col("ghcnd").isNotNull())


def most_recent_ghcn_rows(df):
    latest_window = Window.partitionBy("ghcnd").orderBy(F.col("end_dt").desc())
    latest_per_station = (
        df.withColumn("rn", F.row_number().over(latest_window))
        .filter(F.col("rn") == 1)
        .drop("rn")
    )
    print(f"Rows after collapsing to one per station: {latest_per_station.count():,}")

    filtered_df = latest_per_station.select(
        F.col("ghcnd").alias("station_id"),
        "icao",
        "faa",
        "wmo",
        "country_code",
        "country_name",
        "station_name",
        "end_dt",  # kept for reference/debugging, not necessarily needed downstream
    )
    return filtered_df


def main():
    spark = get_spark()
    session = get_session()

    # Filter out null GCHN rows
    raw_df = spark.table(STATION_EMSHR_RAW_TABLE)
    ghcnd_rows = filter_null_ghcn_rows(raw_df)
    print(f"Rows with GHCND populated: {ghcnd_rows.count():,}")
    print(
        f"Distinct GHCN-D station ids represented: {ghcnd_rows.select('ghcnd').distinct().count():,}"
    )

    # Filter for the most up-to-date rows on each station
    filtered_df = most_recent_ghcn_rows(ghcnd_rows)
    print(f"Rows after filtering to most recent: {filtered_df.count():,}")

    # Write to table
    filtered_df.write.mode("overwrite").saveAsTable(STATION_EMSHR_FILTERED_TABLE)
    print(f"Wrote {filtered_df.count():,} rows to {STATION_EMSHR_FILTERED_TABLE}")


if __name__ == "__main__":
    main()
