# ACIS ingest to get latest yesterday's data, unconverted, native ACIS vocabulary
# Schedule: ACIS updates every morning by 6-8am EST.

"""
Raw ACIS rows as returned: native element codes, units, and string values
("T"/"M"/numeric). Mapping, conversion, and precedence happen downstream.
Docs: https://www.rcc-acis.org/docs_webservices.html
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

from datetime import date, datetime, timedelta, timezone
import pandas as pd
from pyspark.sql.types import StructType, StructField, StringType, DateType, TimestampType  # type: ignore
from wx.utils.common import get_selected_stations, merge_into_target
from wx.utils.config import OBS_ACIS_TABLE
from wx.utils.http_utils import acis_breaker, get_session
from wx.utils.spark_utils import get_spark

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Data source
ACIS_BASE_URL = "http://data.rcc-acis.org/StnData"

# Native ACIS element codes; no confirmed wind element, so AWND is left out
ACIS_ELEMS = ["maxt", "mint", "pcpn", "snow", "snwd"]

# Missing values
MISSING_VALUE = "M"

# For reference only (from a live response, not ACIS's docs); not enforced
ACIS_NATIVE_UNITS = {
    "maxt": "degrees F",
    "mint": "degrees F",
    "pcpn": "inches",
    "snow": "inches",
    "snwd": "inches",
}

TARGET_SCHEMA = StructType(
    [
        StructField("station_id", StringType(), False),
        StructField("obs_date", DateType(), False),
        StructField("variable", StringType(), False),  # native ACIS code, e.g. "maxt"
        StructField("value", StringType(), True),  # exactly as ACIS returned it
        StructField("ingested_at", TimestampType(), False),
    ]
)


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

# --- Step 1: Fetch ACIS data for one station, one day ---


def _fetch_acis_data(session, station_id: str, target_date: date) -> dict:
    """One day of ACIS StnData for a station (no token needed)."""
    params = {
        "sid": station_id,
        "sdate": target_date.isoformat(),
        "edate": target_date.isoformat(),
        "elems": ",".join(ACIS_ELEMS),
        "output": "json",
    }

    @acis_breaker
    def _get():
        response = session.get(ACIS_BASE_URL, params=params, timeout=30)
        response.raise_for_status()
        return response

    return _get().json()


# --- Step 2: Parse into raw rows, values stored exactly as ACIS returned them ---


def _parse_acis_raw(station_id: str, target_date: date, payload: dict) -> pd.DataFrame:
    """One row per station/date/element, raw_value as the untouched string."""

    records = []
    ingested_at = datetime.now(timezone.utc)

    data_rows = payload.get("data", [])
    if not data_rows:
        return pd.DataFrame()

    row = data_rows[0]  # single day requested, exactly one row expected
    values = row[1:]  # first element is the date string, already known

    if all(v == MISSING_VALUE for v in values):
        return pd.DataFrame()

    for acis_elem, raw_value in zip(ACIS_ELEMS, values):
        records.append(
            {
                "station_id": station_id,
                "obs_date": target_date,
                "variable": acis_elem,
                "value": str(raw_value) if raw_value is not None else None,
                "ingested_at": ingested_at,
            }
        )

    return pd.DataFrame.from_records(records)


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------


# Fill a given day (default yesterday), per selected station
def load_acis(spark, session, target_date: date) -> None:
    # Get selected stations
    station_ids = get_selected_stations(spark)

    for station_id in station_ids:
        print(f"{station_id}: fetching ACIS for {target_date} ...")
        payload = _fetch_acis_data(session, station_id, target_date)
        tidy_pdf = _parse_acis_raw(station_id, target_date, payload)
        print(f"{station_id}: parsed {len(tidy_pdf)} rows")

        # Merge into target
        merge_into_target(spark, tidy_pdf, OBS_ACIS_TABLE, TARGET_SCHEMA)
        print(f"{station_id}: merged into {OBS_ACIS_TABLE}")


def main():
    parser = argparse.ArgumentParser(
        description="Ingest ACIS observations for one day."
    )
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=None,
        help="Date to ingest, YYYY-MM-DD (default: yesterday). Note: run before UTC midnight if relying on the default.",
    )
    args, _ = parser.parse_known_args()

    target_date = args.date or (date.today() - timedelta(days=1))

    spark = get_spark()
    session = get_session()

    load_acis(spark, session, target_date)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
