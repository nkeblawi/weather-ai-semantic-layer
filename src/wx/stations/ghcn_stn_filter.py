"""Upserts the WMO-identified stations into cleaned.station_wmo, leaving `selected` as it is."""

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

from pyspark.sql import functions as F  # type: ignore
from delta.tables import DeltaTable  # type: ignore
from wx.utils.config import STATION_LIST_PARSED_TABLE, STATION_WMO_TABLE
from wx.utils.spark_utils import get_spark


def filter_wmo_stations(df):
    """Keep only stations with a non-null WMO id, drop raw lineage columns."""
    return df.filter(F.col("wmo").isNotNull()).drop(
        "raw_line", "source_file", "ingested_at"
    )


def upsert_station_wmo(spark, df_wmo):
    """Creates station_wmo, or upserts on station_id leaving `selected` as it is."""
    if not spark.catalog.tableExists(STATION_WMO_TABLE):
        df_wmo.write.format("delta").saveAsTable(STATION_WMO_TABLE)
        return

    target = DeltaTable.forName(spark, STATION_WMO_TABLE)
    (
        target.alias("t")
        .merge(df_wmo.alias("s"), "t.station_id = s.station_id")
        .whenMatchedUpdate(
            set={
                "latitude": "s.latitude",
                "longitude": "s.longitude",
                "elevation": "s.elevation",
                "state": "s.state",
                "name": "s.name",
                "gsn": "s.gsn",
                "hcn_crn": "s.hcn_crn",
                "wmo": "s.wmo",
                # leave 'selected' off, existing rows keep whatever selected value they already had
            }
        )
        .whenNotMatchedInsertAll()
        .execute()
    )


def main():
    spark = get_spark()

    df = spark.table(STATION_LIST_PARSED_TABLE)
    df_wmo = filter_wmo_stations(df)

    upsert_station_wmo(spark, df_wmo)

    wmo_count = spark.table(STATION_WMO_TABLE).count()
    print(f"Wrote {wmo_count} WMO stations to {STATION_WMO_TABLE}")


if __name__ == "__main__":
    main()
