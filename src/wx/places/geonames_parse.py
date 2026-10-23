"""
Parse weather.raw.geonames_raw's tab-separated lines into
weather.cleaned.places: one row per populated place, with its first-level
division (state/province) and country names joined in, and only the columns
the station lookup uses (place_name is GeoNames' ASCII name).

cities1000.txt columns (GeoNames readme, 0-based): 0 geonameid,
2 asciiname, 4 latitude, 5 longitude, 8 country code, 10 admin1 code,
14 population. admin1CodesASCII.txt: "US.VA" (0), name (1), ASCII name (2).
countryInfo.txt: ISO code (0), country name (4); lines starting "#" are
comments.

Full overwrite on every run, after geonames_fetch.py.
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

from pyspark.sql import functions as F  # type: ignore  # noqa: E402
from wx.utils.config import CATALOG, GEONAMES_RAW_TABLE, PLACES_TABLE, SCHEMA_CLEANED  # noqa: E402
from wx.utils.spark_utils import get_spark  # noqa: E402

CITIES_FILE = "cities1000.txt"
ADMIN1_FILE = "admin1CodesASCII.txt"
COUNTRY_FILE = "countryInfo.txt"


def null_if_blank(col):
    """A blank string column as NULL."""
    return F.when(F.trim(col) == "", None).otherwise(F.trim(col))


def fields(raw, source_file):
    """The lines of one source file, split on tabs into an array column f."""
    return (
        raw.filter(F.col("source_file") == source_file)
        .filter(~F.col("raw_line").startswith("#"))
        .withColumn("f", F.split("raw_line", "\t"))
    )


def parse_places(raw):
    cities = fields(raw, CITIES_FILE).select(
        F.col("f")[0].cast("bigint").alias("geonameid"),
        F.col("f")[2].alias("place_name"),
        F.col("f")[4].cast("double").alias("latitude"),
        F.col("f")[5].cast("double").alias("longitude"),
        F.col("f")[8].alias("country_code"),
        null_if_blank(F.col("f")[10]).alias("admin1_code"),
        F.col("f")[14].cast("bigint").alias("population"),
    )
    admin1 = fields(raw, ADMIN1_FILE).select(
        F.split(F.col("f")[0], r"\.")[0].alias("country_code"),
        F.split(F.col("f")[0], r"\.")[1].alias("admin1_code"),
        F.col("f")[2].alias("admin1_name"),  # the ASCII name, matched like place_name
    )
    countries = fields(raw, COUNTRY_FILE).select(
        F.col("f")[0].alias("country_code"),
        F.col("f")[4].alias("country_name"),
    )
    return (
        cities.join(admin1, ["country_code", "admin1_code"], "left")
        .join(countries, ["country_code"], "left")
        .filter(F.col("latitude").isNotNull() & F.col("longitude").isNotNull())
        .select(
            "geonameid", "place_name", "country_code", "country_name", "admin1_code", "admin1_name",
            "latitude", "longitude", "population",
        )
    )


def main():
    spark = get_spark()
    places = parse_places(spark.table(GEONAMES_RAW_TABLE))

    spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA_CLEANED}")
    places.write.format("delta").mode("overwrite").saveAsTable(PLACES_TABLE)
    table = spark.table(PLACES_TABLE)
    print(f"Wrote {table.count()} places to {PLACES_TABLE}")
    print(f"  without a country name: {table.filter(F.col('country_name').isNull()).count()}")


if __name__ == "__main__":
    main()
