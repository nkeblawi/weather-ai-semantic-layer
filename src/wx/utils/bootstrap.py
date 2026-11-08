"""
Sets WX_CATALOG from the job's --catalog argument. Call it after the
script's sys.path line and before importing wx.utils.config, which reads
WX_CATALOG at import time.
"""

import os
import argparse


def bootstrap():
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", required=True)  # weather (prod) or weather_dev; no default
    args, _ = parser.parse_known_args()
    os.environ["WX_CATALOG"] = args.catalog
