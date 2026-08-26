"""
Fetch the EMSHR station list from NOAA and land it as raw, unparsed
lines in weather.raw.emshr_lite_raw. We need this in order to get
ICAO, FAA, and country codes and merge them into the stations table.
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

from wx.utils.config import STATION_EMSHR_RAW_TABLE
from datetime import datetime, timezone
from pyspark.sql import functions as F  # type: ignore
from wx.utils.http_utils import get_session, emshr_breaker
from wx.utils.spark_utils import get_spark

# Set source file and path
SOURCE_FILE = "emshr_lite.txt"
SOURCE_URL = f"https://www.ncei.noaa.gov/access/homr/file/{SOURCE_FILE}"


def fetch_station_list(session) -> str:
    """Fetch the raw station list file contents from NOAA."""

    @emshr_breaker
    def _get():
        response = session.get(SOURCE_URL, timeout=30)
        response.raise_for_status()
        return response

    return _get().content.decode("latin-1")


def get_fields():
    return [
        ("ncdc", 1, 8),  # - unique NCEI station-history record ID
        ("beg_dt", 10, 17),  # - record begin date, YYYYMMDD
        ("end_dt", 19, 26),  # - record end date, YYYYMMDD (99991231 = currently open)
        ("icao", 41, 44),  # - ICAO station ID, null if not available
        ("faa", 46, 50),  # - FAA station ID, null if not available
        ("wmo", 58, 62),  # - WMO station ID, null if not available
        ("ghcnd", 75, 85),  # - GHCN-Daily station ID,
        ("station_name", 87, 186),  # - Name of station
        ("country_code", 188, 189),  # - FIPS country code (NOT ISO 3166-1
        ("country_name", 191, 225),  # - Name of Country
    ]


def main():
    spark = get_spark()
    session = get_session()

    raw_station_list = fetch_station_list(session)
    lines = raw_station_list.splitlines()

    data_lines = [ln for ln in lines[2:] if ln.strip()]
    lines_df = spark.createDataFrame([(ln,) for ln in data_lines], ["line"])

    fields = get_fields()
    select_exprs = [
        F.trim(F.substring(F.col("line"), start, end - start + 1)).alias(name)
        for name, start, end in fields
    ]
    parsed_df = lines_df.select(*select_exprs)

    # Empty-string vs null gets inconsistent once you start filtering/joining on these columns --
    # normalize blanks to null right away
    for name, _, _ in fields:
        parsed_df = parsed_df.withColumn(
            name, F.when(F.col(name) == "", None).otherwise(F.col(name))
        )
    print(f"Parsed {parsed_df.count():,} total station-history records")

    # Write to raw table
    parsed_df.write.mode("overwrite").saveAsTable(STATION_EMSHR_RAW_TABLE)
    print(f"Wrote {parsed_df.count():,} rows to {STATION_EMSHR_RAW_TABLE}")

    print(f"Done at {datetime.now(timezone.utc)}")


if __name__ == "__main__":
    main()
