"""
Merge the filtered EMSHR stations with WMO stations into a merged station table.
Existing rows are upserted on station_id, not overwritten, so that
station_merged.selected/city (set by hand in activate_station.py) survive a
rerun; same principle ghcn_stn_filter.py already applies for
station_wmo.selected.
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
    STATION_WMO_TABLE,
    STATION_EMSHR_FILTERED_TABLE,
    STATION_MERGED_TABLE,
)
from delta.tables import DeltaTable  # type: ignore
from pyspark.sql import functions as F  # type: ignore
from wx.utils.http_utils import get_session
from wx.utils.spark_utils import get_spark


def merge_icao(wmo_df, emshr_df):
    """
    Join WMO stations with EMSHR-derived ICAO/FAA/country columns. Drops
    station_wmo's own `selected` column first; it's a different flag from
    station_merged's `selected` (set by activate_station.py), and there's no
    reason for this merge to touch it either way.
    """
    stations_df = (
        wmo_df.drop("selected")
        .alias("sw")
        .join(emshr_df.alias("em"), on="station_id", how="left")
        .select("sw.*", "em.icao", "em.faa", "em.country_code", "em.country_name")
        .withColumn("selected", F.lit(False))
        .withColumn("city", F.lit(None).cast("string"))
    )
    return stations_df


def upsert_station_merged(spark, merged_df, target_table):
    """
    Create station_merged if it doesn't exist yet, otherwise merge/upsert on
    station_id, leaving `selected`/`city` untouched for existing rows so
    manual activation (activate_station.py) survives a rerun.
    """
    preserved_columns = {"station_id", "selected", "city"}
    update_columns = [c for c in merged_df.columns if c not in preserved_columns]

    if not spark.catalog.tableExists(target_table):
        merged_df.write.format("delta").saveAsTable(target_table)
        return

    target = DeltaTable.forName(spark, target_table)
    (
        target.alias("t")
        .merge(merged_df.alias("s"), "t.station_id = s.station_id")
        .whenMatchedUpdate(set={c: f"s.{c}" for c in update_columns})
        .whenNotMatchedInsertAll()
        .execute()
    )


def main():
    spark = get_spark()
    session = get_session()

    # Read data from WMO and EMSHR tables
    station_wmo_df = spark.table(STATION_WMO_TABLE)
    emshr_df = spark.table(STATION_EMSHR_FILTERED_TABLE).select(
        "station_id", "icao", "faa", "country_code", "country_name"
    )

    print(f"{STATION_WMO_TABLE}: {station_wmo_df.count():,} rows")
    print(f"{STATION_EMSHR_FILTERED_TABLE}: {emshr_df.count():,} rows")

    # Merge ICAO, FAA, and Country columns into GHNC-D/WMO data
    merged_df = merge_icao(station_wmo_df, emshr_df)
    print(f"{STATION_MERGED_TABLE}: {merged_df.count():,} rows")

    total = merged_df.count()
    icao_count = merged_df.filter(F.col("icao").isNotNull()).count()
    us_icao_count = merged_df.filter(
        F.col("icao").isNotNull() & (F.col("country_code") == "US")
    ).count()

    print(f"Total stations: {total:,}")
    print(f"Stations with ICAO populated: {icao_count:,}")
    print(f"Of which, US stations: {us_icao_count:,}")

    # Merge/upsert into table, on station_id, so selected/city survive a rerun
    upsert_station_merged(spark, merged_df, STATION_MERGED_TABLE)
    print(f"Upserted {total:,} rows into {STATION_MERGED_TABLE}")


if __name__ == "__main__":
    main()
