# Historical data loader: GHCN-Daily .dly files -> weather.raw.obs_ghcn

# Schedule: GHCN-Daily updates daily by 2 PM EST

"""
Ingests GHCN-Daily .dly files into raw.obs_ghcn. By default it fetches only
each file's tail (an HTTP Range request); --full downloads whole files, and
with --station-id backfills one newly activated station.
Docs: https://www.ncei.noaa.gov/pub/data/ghcn/daily/readme.txt
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

from datetime import datetime, timezone
import pandas as pd
from pyspark.sql.types import StructType, StructField, StringType, DateType, DoubleType, TimestampType
from wx.utils.common import get_selected_stations, merge_into_target
from wx.utils.config import OBS_GHCN_TABLE
from wx.utils.http_utils import ghcn_breaker, get_session
from wx.utils.spark_utils import get_spark

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DLY_URL_TEMPLATE = "https://www.ncei.noaa.gov/pub/data/ghcn/daily/all/{station_id}.dly"

# Variables selected for this project
VARIABLES = {
    "PRCP",  # [CORE] Precipitation (tenths of mm)
    "SNOW",  # [CORE] Snowfall (mm)
    "SNWD",  # [CORE] Snow depth (mm)
    "TMAX",  # [CORE] Maximum temperature (tenths of degrees C)
    "TMIN",  # [CORE] Minimum temperature (tenths of degrees C)
    "AWND",  # Average wind speed (tenths of meters per second)
}

# Missing values
MISSING_VALUE = -9999

# Tail byte size for daily ingest
TAIL_BYTES = 7500

# Target schema
TARGET_SCHEMA = StructType(
    [
        StructField("station_id", StringType(), False),
        StructField("obs_date", DateType(), False),
        StructField("variable", StringType(), False),
        StructField("value", DoubleType(), True),
        StructField("mflag", StringType(), True),
        StructField("qflag", StringType(), True),
        StructField("sflag", StringType(), True),
        StructField("ingested_at", TimestampType(), False),
    ]
)


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

# --- Step 1: Fetch GHCN-Daily data for one station ---


def download_dly(session, station_id: str, tail_bytes: int | None = None) -> str:
    """
    A station's .dly file, or only its last tail_bytes via a Range request.
    A server that ignores Range sends the whole file (200, not 206), which
    still parses.
    """

    url = DLY_URL_TEMPLATE.format(station_id=station_id)
    headers = {"Range": f"bytes=-{tail_bytes}"} if tail_bytes else {}

    @ghcn_breaker
    def _get():
        response = session.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        return response

    response = _get()
    print(f"Request status: {response.status_code}")

    text = response.text
    if tail_bytes:
        print(
            f"({'partial content' if response.status_code == 206 else 'full file, Range ignored'})"
        )
        # First line is likely cut off mid-record at the byte boundary; discard it.
        text = text.split("\n", 1)[1] if "\n" in text else ""

    return text


# --- Step 2: parse fixed-width .dly into rows ---


def parse_dly(station_id: str, raw_text: str) -> pd.DataFrame:
    """
    .dly fixed-width text as one row per station/date/element: raw integers,
    -9999 as NULL, flags kept (QC and units are downstream).

    Field layout (readme.txt Section III), 0-indexed slice offsets:
      ID       line[0:11]
      YEAR     line[11:15]
      MONTH    line[15:17]
      ELEMENT  line[17:21]
      then 31 repeating 8-char day blocks starting at offset 21:
        VALUE  block[0:5]
        MFLAG  block[5:6]
        QFLAG  block[6:7]
        SFLAG  block[7:8]
    """

    records = []
    ingested_at = datetime.now(timezone.utc)

    for line in raw_text.splitlines():
        if not line.strip():
            continue

        element = line[17:21].strip()
        if element not in VARIABLES:
            continue

        year = int(line[11:15])
        month = int(line[15:17])

        for day in range(1, 32):
            offset = 21 + (day - 1) * 8
            block = line[offset : offset + 8]
            if len(block) < 8:
                continue

            raw_value = block[0:5].strip()
            mflag = block[5:6].strip() or None
            qflag = block[6:7].strip() or None
            sflag = block[7:8].strip() or None

            try:
                int_value = int(raw_value)
            except ValueError:
                continue

            # Days that don't exist (e.g. April 31) are -9999 padding, not missing data
            try:
                obs_date = datetime(year, month, day).date()
            except ValueError:
                continue

            # A real day's -9999 is missing: NULL; everything else passes through unscaled
            value = None if int_value == MISSING_VALUE else float(int_value)

            # Append data to records
            records.append(
                {
                    "station_id": station_id,
                    "obs_date": obs_date,
                    "variable": element,
                    "value": value,
                    "mflag": mflag,
                    "qflag": qflag,
                    "sflag": sflag,
                    "ingested_at": ingested_at,
                }
            )

    return pd.DataFrame.from_records(records)


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------


def load_ghcn(
    spark, session, tail_bytes: int | None, only_station_id: str | None = None
) -> None:
    """
    tail_bytes: None for whole files, else just the tail.
    only_station_id: that station only, selected or not (backfill); else all selected.
    """

    if only_station_id:
        station_ids = [only_station_id]
    else:
        station_ids = get_selected_stations(spark)
    mode = f"tail-only ({tail_bytes} bytes)" if tail_bytes else "full file"

    print(
        f"Loading GHCN data for {len(station_ids)} stations, mode={mode}: {station_ids}"
    )

    for station_id in station_ids:
        print(f"Downloading {station_id}.dly...")
        raw_text = download_dly(session, station_id, tail_bytes=tail_bytes)

        print(f"Parsing {station_id}.dly...")
        df = parse_dly(station_id, raw_text)
        print(f"Parsed {len(df)} rows for {station_id}")

        print(f"Merging {station_id}.dly into {OBS_GHCN_TABLE}...")
        # A missing day never erases a value already in raw (e.g. from an older station ID)
        merge_into_target(
            spark, df, OBS_GHCN_TABLE, TARGET_SCHEMA, keep_existing_values=True
        )

    print("Done.")


def main():
    parser = argparse.ArgumentParser(description="Ingest GHCN-Daily observations.")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Download the full .dly file instead of just the tail bytes.",
    )
    parser.add_argument(
        "--tail-bytes",
        type=int,
        default=TAIL_BYTES,
        help=f"Tail byte size for Range requests (default {TAIL_BYTES}). Ignored if --full is set.",
    )
    parser.add_argument(
        "--station-id",
        default=None,
        help=(
            "Ingest only this GHCN station_id instead of every selected station. "
            "Combine with --full to backfill a newly activated station's full history."
        ),
    )
    args, _ = parser.parse_known_args()

    spark = get_spark()
    session = get_session()

    tail_bytes = None if args.full else args.tail_bytes
    only_station_id = args.station_id or None
    load_ghcn(
        spark, session, tail_bytes=tail_bytes, only_station_id=only_station_id
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
