"""
Reads --catalog, passed once per target via databricks.yml's catalog
variable (dev: weather_dev, prod: weather -- see resources/*.yml's
spark_python_task parameters), and sets WX_CATALOG so wx.utils.config picks
up the right catalog for whichever target deployed this job.

Call this only after the script's line:

    sys.path.append(str(Path(__file__).resolve().parent.parent.parent))

That's what makes `wx` importable at all, so it can't live here -- every
script under wx/stations/ or wx/obs/ needs that one line before it can
reach this module, since bootstrap() must run before wx.utils.config is
imported (CATALOG there is read from WX_CATALOG at import time).
"""

import os
import argparse


def bootstrap():
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", default="weather")
    args, _ = parser.parse_known_args()
    os.environ["WX_CATALOG"] = args.catalog
