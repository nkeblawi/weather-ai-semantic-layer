"""Fetches NOAA's GHCN-Daily station list into raw.station_list_raw, overwriting it."""

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

from wx.utils.config import STATION_LIST_RAW_TABLE
from datetime import datetime, timezone
from pyspark.sql import Row  # type: ignore
from wx.utils.http_utils import get_session, ghcn_breaker
from wx.utils.spark_utils import get_spark

SOURCE_FILE = "ghcnd-stations.txt"
SOURCE_URL = f"https://www.ncei.noaa.gov/pub/data/ghcn/daily/{SOURCE_FILE}"


def fetch_station_list(session) -> str:
    """Fetch the raw station list file contents from NOAA."""

    @ghcn_breaker
    def _get():
        response = session.get(SOURCE_URL, timeout=30)
        response.raise_for_status()
        return response

    return _get().text


def main():
    spark = get_spark()
    session = get_session()

    raw_station_list = fetch_station_list(session)
    lines = raw_station_list.splitlines()
    print(f"Fetched {len(lines)} stations from {SOURCE_URL}")

    ingested_at = datetime.now(timezone.utc)
    print(f"ingested_at: {ingested_at}")

    stations = [
        Row(raw_line=line, source_file=SOURCE_FILE, ingested_at=ingested_at)
        for line in lines
    ]

    df = spark.createDataFrame(stations)
    df.write.format("delta").mode("overwrite").saveAsTable(STATION_LIST_RAW_TABLE)

    station_count = spark.table(STATION_LIST_RAW_TABLE).count()
    print(f"Wrote {station_count} stations to {STATION_LIST_RAW_TABLE}")


if __name__ == "__main__":
    main()
