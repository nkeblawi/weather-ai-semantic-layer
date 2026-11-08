"""
Recreates analytics.places, where station lookup finds a town's coordinates:
GeoNames places with 1,000+ people and a lowercase name_key. Last task of
wx_places_ingest.
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

from wx.utils.config import PLACES_TABLE, PLACES_VIEW
from wx.utils.spark_utils import get_spark


def main():
    spark = get_spark()
    spark.sql(
        f"""
        CREATE OR REPLACE VIEW {PLACES_VIEW} AS
        SELECT
          geonameid,
          place_name,
          lower(place_name) AS name_key,
          country_code,
          country_name,
          admin1_code,
          admin1_name,
          latitude,
          longitude,
          population
        FROM {PLACES_TABLE}
        """
    )
    print(f"Recreated {PLACES_VIEW}")


if __name__ == "__main__":
    main()
