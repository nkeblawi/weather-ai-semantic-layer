# Historical data loader: GHCN-Daily .dly files -> weather.raw.obs_ghcn

# Schedule: GHCN-Daily updates daily by 2 PM EST

"""
Converted from the OBS_01_GHCN_Daily_Ingest notebook. Schedule: GHCN-Daily
updates daily by 2 PM EST.

Uses tail-byte HTTP Range requests by default (empirically confirmed
tail_bytes=8000 covers a full day's records without re-downloading entire
multi-decade station histories), with --full available for a complete
re-download when needed.
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

from wx.utils.bootstrap import bootstrap  # noqa: E402
bootstrap()

import argparse
from datetime import datetime, timezone
import pandas as pd
from pyspark.sql.types import StructType, StructField, StringType, DateType, DoubleType, TimestampType  # type: ignore
from wx.utils.common import get_selected_stations, merge_into_target
from wx.utils.config import OBS_GHCN_TABLE
from wx.utils.http_utils import ghcn_breaker, get_session
from wx.utils.spark_utils import get_spark

# ---------------------------------------------------------------------------
# Config
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
# Step 1: Fetch GHCN-Daily data for one station
# ---------------------------------------------------------------------------


def download_dly(session, station_id: str, tail_bytes: int | None = None) -> str:
    """
    Download the .dly file for a station. If tail_bytes is provided, uses an
    HTTP Range request to fetch only the last N bytes instead of the whole
    file, useful at higher station counts where re-downloading full
    multi-decade histories daily gets expensive.

    If the server doesn't honor Range (ignores it and returns 200 with the
    full file), this still works correctly, just without the bandwidth
    savings, check response.status_code (206 = partial content actually
    returned, 200 = full file sent regardless).
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


# ---------------------------------------------------------------------------
# Step 2: parse fixed-width .dly into rows
# ---------------------------------------------------------------------------


def parse_dly(station_id: str, raw_text: str) -> pd.DataFrame:
    """
    Parse GHCN-D .dly fixed-width text into a tidy (long) DataFrame:
    one row per station/date/element, values UNSCALED (raw source integers,
    with -9999 mapped to NULL). No QC filtering, QFLAG-failed rows are
    kept with their flag intact. Unit conversion and QC-based filtering are
    downstream (cleaned layer) decisions, not raw ingest decisions.

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

            # Calendar-nonexistent days (e.g. VALUE31 for April) are always
            # -9999 AND fail date construction below. This is a structural
            # artifact of the fixed-width format, not a real station-day.
            try:
                obs_date = datetime(year, month, day).date()
            except ValueError:
                continue

            # True missing readings (for resolved calendar days):
            # map -9999 to NULL.
            # Everything else passes through unscaled, regardless of QFLAG.
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
# Orchestration
# ---------------------------------------------------------------------------


def load_ghcn(spark, session, tail_bytes: int | None) -> None:
    """
    tail_bytes=None: download and parse the full .dly file.
    tail_bytes=N: Range-request just the tail (default DEFAULT_TAIL_BYTES).
    """

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
        merge_into_target(spark, df, OBS_GHCN_TABLE, TARGET_SCHEMA)

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
    args, _ = parser.parse_known_args()

    spark = get_spark()
    session = get_session()

    tail_bytes = None if args.full else args.tail_bytes
    load_ghcn(spark, session, tail_bytes=tail_bytes)


if __name__ == "__main__":
    main()
