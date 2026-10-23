"""
Fetch the GeoNames gazetteer files and land them as raw, unparsed lines in
weather.raw.geonames_raw (one row per line, tagged with its source file):

    cities1000.txt         every populated place with 1,000+ people (from cities1000.zip)
    admin1CodesASCII.txt   first-level division names (US states, Canadian provinces, ...)
    countryInfo.txt        country names

GeoNames (https://www.geonames.org) is licensed CC BY 4.0: credit it
wherever its data is shown.

Full overwrite on every run (a new snapshot of GeoNames, nothing to merge).
Runs only when the wx_places_ingest job is started by hand.
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

import io  # noqa: E402
import zipfile  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pyspark.sql import Row  # type: ignore  # noqa: E402
from wx.utils.config import GEONAMES_RAW_TABLE  # noqa: E402
from wx.utils.http_utils import geonames_breaker, get_session  # noqa: E402
from wx.utils.spark_utils import get_spark  # noqa: E402

BASE_URL = "https://download.geonames.org/export/dump"
CITIES_ZIP, CITIES_FILE = "cities1000.zip", "cities1000.txt"
ADMIN1_FILE = "admin1CodesASCII.txt"
COUNTRY_FILE = "countryInfo.txt"


def fetch(session, file_name: str) -> bytes:
    """One file from the GeoNames dump."""

    @geonames_breaker
    def _get():
        response = session.get(f"{BASE_URL}/{file_name}", timeout=300)
        response.raise_for_status()
        return response

    return _get().content


def source_lines(session) -> dict[str, list[str]]:
    """source file name -> its lines, the cities file unzipped."""
    with zipfile.ZipFile(io.BytesIO(fetch(session, CITIES_ZIP))) as archive:
        cities = archive.read(CITIES_FILE).decode("utf-8")
    return {
        CITIES_FILE: cities.splitlines(),
        ADMIN1_FILE: fetch(session, ADMIN1_FILE).decode("utf-8").splitlines(),
        COUNTRY_FILE: fetch(session, COUNTRY_FILE).decode("utf-8").splitlines(),
    }


def main():
    spark = get_spark()
    session = get_session()

    ingested_at = datetime.now(timezone.utc)
    rows = []
    for source_file, lines in source_lines(session).items():
        print(f"Fetched {len(lines)} lines of {source_file}")
        rows += [Row(raw_line=line, source_file=source_file, ingested_at=ingested_at) for line in lines]

    df = spark.createDataFrame(rows)
    df.write.format("delta").mode("overwrite").saveAsTable(GEONAMES_RAW_TABLE)
    print(f"Wrote {spark.table(GEONAMES_RAW_TABLE).count()} lines to {GEONAMES_RAW_TABLE}")


if __name__ == "__main__":
    main()
