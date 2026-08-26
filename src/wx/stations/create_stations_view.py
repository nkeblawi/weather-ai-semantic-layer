"""
(Re)creates weather.analytics.stations, the view lookup_station (the
Unity Catalog function the weather-query-resolver skill calls) reads from --
active (selected=true), WMO-identified stations only, with just the columns
the app needs.

Not part of any scheduled job on its own; runs as the update_stations_table
task in wx_activate_station.job.yml, right after activate_station.py changes
station_merged.selected/city, so the view reflects the change immediately.

Previously a Databricks-UI-managed saved Query (sql_task + query_id), not
version controlled. Moved here so it's a script like everything else in
this pipeline, and so --catalog can parameterize it the same way instead of
needing IDENTIFIER(:catalog || ...) SQL-side parameterization.
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

from wx.utils.config import STATION_MERGED_TABLE, STATIONS_VIEW  # noqa: E402
from wx.utils.spark_utils import get_spark  # noqa: E402


def main():
    spark = get_spark()
    spark.sql(
        f"""
        CREATE OR REPLACE VIEW {STATIONS_VIEW} AS
        SELECT
          station_id AS ghcn_id,
          icao,
          faa,
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


if __name__ == "__main__":
    main()
