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
    STATION_CODES_TABLE,
    STATION_WMO_TABLE,
    STATION_EMSHR_FILTERED_TABLE,
    STATION_MERGED_TABLE,
)
from delta.tables import DeltaTable  # type: ignore
from pyspark.sql import functions as F  # type: ignore
from wx.utils.http_utils import get_session, ghcn_breaker
from wx.utils.spark_utils import get_spark

COUNTRIES_URL = "https://www.ncei.noaa.gov/pub/data/ghcn/daily/ghcnd-countries.txt"


def merge_icao(wmo_df, emshr_df, codes_df, countries_df):
    """
    Join WMO stations with EMSHR-derived ICAO/FAA/country columns. Drops
    station_wmo's own `selected` column first; it's a different flag from
    station_merged's `selected` (set by activate_station.py), and there's no
    reason for this merge to touch it either way.

    EMSHR covers US stations only, so other stations get their codes and
    country from elsewhere:
      - icao / iata: cleaned.station_codes (hand-maintained, seeded from
        scripts/station_codes.sql) wins where it has a row; otherwise EMSHR's
        icao, and EMSHR's FAA code as the IATA code (the same for US
        commercial airports).
      - country_code: EMSHR, else the GHCN ID's first two letters (its FIPS
        country code, e.g. "CA" in CAN07025251).
      - country_name: EMSHR, else NOAA's ghcnd-countries.txt (upper-cased to
        match EMSHR's "UNITED STATES" style).
    """
    prefix = F.substring(F.col("sw.station_id"), 1, 2)
    stations_df = (
        wmo_df.drop("selected")
        .alias("sw")
        .join(emshr_df.alias("em"), on="station_id", how="left")
        .join(codes_df.alias("sc"), on="station_id", how="left")
        .join(countries_df.alias("ct"), prefix == F.col("ct.code"), how="left")
        .select(
            "sw.*",
            F.coalesce(F.col("sc.icao"), F.col("em.icao")).alias("icao"),
            F.col("em.faa").alias("faa"),
            F.coalesce(F.col("sc.iata"), F.col("em.faa")).alias("iata"),
            F.coalesce(F.col("em.country_code"), prefix).alias("country_code"),
            F.coalesce(F.col("em.country_name"), F.upper(F.col("ct.name"))).alias(
                "country_name"
            ),
        )
        .withColumn("selected", F.lit(False))
        .withColumn("city", F.lit(None).cast("string"))
    )
    return stations_df


def read_station_codes(spark):
    """station_codes rows, or an empty frame if the table doesn't exist yet."""
    if spark.catalog.tableExists(STATION_CODES_TABLE):
        return spark.table(STATION_CODES_TABLE).select("station_id", "icao", "iata")
    return spark.createDataFrame([], "station_id string, icao string, iata string")


def fetch_countries(spark, session):
    """NOAA's FIPS country code -> name list (ghcnd-countries.txt)."""

    @ghcn_breaker
    def _get():
        response = session.get(COUNTRIES_URL, timeout=30)
        response.raise_for_status()
        return response

    rows = [
        (line[0:2], line[3:].strip())
        for line in _get().text.splitlines()
        if len(line) > 3
    ]
    return spark.createDataFrame(rows, "code string, name string")


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

    # Columns added since the table was created (e.g. iata) are added to it
    # first, so the MERGE can set them.
    existing = set(spark.table(target_table).columns)
    for field in merged_df.schema.fields:
        if field.name not in existing:
            spark.sql(
                f"ALTER TABLE {target_table} ADD COLUMNS ({field.name} {field.dataType.simpleString()})"
            )
            print(f"Added column {field.name} to {target_table}")

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

    codes_df = read_station_codes(spark)
    countries_df = fetch_countries(spark, session)

    # Merge ICAO, FAA/IATA, and Country columns into GHNC-D/WMO data
    merged_df = merge_icao(station_wmo_df, emshr_df, codes_df, countries_df)
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
