"""
Activates one station in station_merged (selected=true) and/or sets its city,
which no source provides. Run on demand by wx_activate_station, e.g.:

    --station-id USW00014739 --activate --city "Boston"

--activate needs an airport code (icao or iata): for non-US stations, add it
to sql/maintenance/station_codes.sql and rerun wx_station_ingest first.
"""

import os
import sys
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--workspace-root", default=os.environ.get("WX_WORKSPACE_ROOT"))
args, _ = parser.parse_known_args()
if not args.workspace_root:
    parser.error(
        "--workspace-root is required (or set WX_WORKSPACE_ROOT for manual/notebook testing)"
    )
sys.path.append(args.workspace_root)

from wx.utils.bootstrap import bootstrap

bootstrap()

from wx.utils.config import STATION_MERGED_TABLE
from wx.utils.spark_utils import get_spark

# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------


def _check_airport_code(spark, station_id: str) -> None:
    """Refuses a station with no airport code (iata, or faa on tables built before iata)."""
    code_col = "iata" if "iata" in spark.table(STATION_MERGED_TABLE).columns else "faa"
    row = spark.sql(
        f"SELECT icao, {code_col} AS code FROM {STATION_MERGED_TABLE} WHERE station_id = :station_id",
        args={"station_id": station_id},
    ).first()
    if row["icao"] is None and row["code"] is None:
        raise ValueError(
            f'"{station_id}" has no ICAO/IATA code: add it to sql/maintenance/station_codes.sql, '
            "run that file, rerun wx_station_ingest, then activate."
        )


def _update_station(spark, station_id: str, activate: bool, city: str | None):
    """Sets selected and/or city on station_id; ValueError if it doesn't exist."""
    exists = (
        spark.sql(
            f"SELECT 1 FROM {STATION_MERGED_TABLE} WHERE station_id = :station_id",
            args={"station_id": station_id},
        )
        .limit(1)
        .count()
    )
    if not exists:
        raise ValueError(f'No station with station_id "{station_id}".')

    if activate:
        _check_airport_code(spark, station_id)

    set_clauses = []
    args = {"station_id": station_id}
    if activate:
        set_clauses.append("selected = true")
    if city is not None:
        set_clauses.append("city = :city")
        args["city"] = city

    spark.sql(
        f"""
            UPDATE {STATION_MERGED_TABLE}
            SET {", ".join(set_clauses)}
            WHERE station_id = :station_id
        """,
        args=args,
    )


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Activate a station and/or set its city, by GHCN station_id."
    )

    parser.add_argument(
        "--station-id",
        required=True,
        help='GHCN station_id, e.g. "USW00093738". Look it up first in station_merged.',
    )
    parser.add_argument("--activate", action="store_true", help="Set selected=true.")
    parser.add_argument("--city", default=None, help='City to set, e.g. "New York".')

    args, _ = parser.parse_known_args()
    # The job passes --city "" when blank: keep the existing city
    args.city = (args.city or "").strip() or None

    if not args.activate and args.city is None:
        parser.error("Nothing to do: pass --activate and/or --city.")

    spark = get_spark()

    try:
        _update_station(spark, args.station_id, args.activate, args.city)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    row = spark.sql(
        f"SELECT * FROM {STATION_MERGED_TABLE} WHERE station_id = :station_id",
        args={"station_id": args.station_id},
    )
    row.show(truncate=False)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
