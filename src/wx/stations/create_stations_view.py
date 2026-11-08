"""
Recreates analytics.stations, the view lookup_station reads: active,
WMO-identified stations with the columns the app needs. Runs in
wx_activate_station after activate_station.py.
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

from wx.utils.config import STATION_MERGED_TABLE, STATIONS_VIEW
from wx.utils.spark_utils import get_spark

# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------


def main():
    spark = get_spark()
    spark.sql(
        f"""
        CREATE OR REPLACE VIEW {STATIONS_VIEW} AS
        SELECT
          station_id AS ghcn_id,
          icao,
          iata,
          name,
          city,
          state,
          country_name,
          country_code,
          wmo,
          latitude,
          longitude,
          elevation
        FROM {STATION_MERGED_TABLE}
        WHERE selected = true
          AND wmo IS NOT NULL
        """
    )
    print(f"Recreated {STATIONS_VIEW}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
