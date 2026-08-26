"""
Manually update one station in station_merged, by its GHCN station_id:
activate it (selected=true) and/or set its city. station_merged has no city
column derived from source data (only the raw `name` and `state`), so city
is always set by hand here.

station_id is a unique key, so every update here touches at most one row --
no ambiguous-match handling needed. Look up the station_id first, e.g.:

    SELECT station_id, name, icao, faa
    FROM weather.cleaned.station_merged
    WHERE contains(upper(name), 'DULLES')

This is NOT part of any scheduled job. It is run on demand with usage:

    python activate_station.py --station-id USW00093738 --activate --city "Sterling, VA"
    python activate_station.py --station-id USW00093738 --activate            # no city yet
    python activate_station.py --station-id USW00093738 --city "Sterling, VA" # fix city on an already-active station

Uses parameterized SQL (args=) to avoid string interpolation for
user-supplied values, matching the pattern already used elsewhere in this
project.
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

from wx.utils.config import STATION_MERGED_TABLE
from wx.utils.spark_utils import get_spark


def update_station(spark, station_id: str, activate: bool, city: str | None):
    """
    Set selected=true (if activate) and/or city (if provided) on the
    station matching station_id. Errors if station_id doesn't exist.
    """

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
    parser.add_argument(
        "--city", default=None, help='City to set, e.g. "Sterling, VA".'
    )

    args, _ = parser.parse_known_args()

    if not args.activate and args.city is None:
        parser.error("Nothing to do: pass --activate and/or --city.")

    spark = get_spark()

    try:
        update_station(spark, args.station_id, args.activate, args.city)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    row = spark.sql(
        f"SELECT * FROM {STATION_MERGED_TABLE} WHERE station_id = :station_id",
        args={"station_id": args.station_id},
    )
    row.show(truncate=False)


if __name__ == "__main__":
    main()
