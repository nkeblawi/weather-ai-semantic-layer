"""
Central configuration for the weather pipeline: catalog, schema, and table
name constants. Every ingest/transform script should import from here rather
than hardcoding these strings, so a rename only has to happen in one place.

Names follow the productionization plan (see Productionize `Projects/Weather`
notebooks doc), not the older "silver" naming used in earlier notes.
"""

import os

# --- Catalog ---
# Overridable via WX_CATALOG so dev jobs can point at a separate mirror
# catalog (e.g. weather_dev) instead of writing to the same tables as prod.
# Must be set (e.g. via a --catalog CLI arg setting os.environ) before this
# module is imported, since every table constant below is computed once at
# import time.
CATALOG = os.environ.get("WX_CATALOG", "weather")

# --- Schemas ---
SCHEMA_RAW = "raw"
SCHEMA_CLEANED = "cleaned"
SCHEMA_ANALYTICS = "analytics"

# --- Station tables ---
STATION_LIST_RAW_TABLE = f"{CATALOG}.{SCHEMA_RAW}.station_list_raw"
STATION_LIST_PARSED_TABLE = f"{CATALOG}.{SCHEMA_CLEANED}.station_list_parsed"
STATION_WMO_TABLE = f"{CATALOG}.{SCHEMA_CLEANED}.station_wmo"
STATION_EMSHR_RAW_TABLE = f"{CATALOG}.{SCHEMA_RAW}.emshr_lite_raw"
STATION_EMSHR_FILTERED_TABLE = f"{CATALOG}.{SCHEMA_CLEANED}.emshr_filtered"
STATION_MERGED_TABLE = f"{CATALOG}.{SCHEMA_CLEANED}.station_merged"

# --- Observation tables ---
OBS_ACIS_TABLE = f"{CATALOG}.{SCHEMA_RAW}.obs_acis"
OBS_GHCN_TABLE = f"{CATALOG}.{SCHEMA_RAW}.obs_ghcn"
OBS_ACIS_CONV_TABLE = f"{CATALOG}.{SCHEMA_CLEANED}.obs_acis_conv"
OBS_GHCN_CONV_TABLE = f"{CATALOG}.{SCHEMA_CLEANED}.obs_ghcn_conv"
OBS_MERGED_TABLE = f"{CATALOG}.{SCHEMA_CLEANED}.obs_merged"
OBS_PIVOT_TABLE = f"{CATALOG}.{SCHEMA_CLEANED}.obs_pivot"

# --- Analytics tables ---
OBS_ANALYTICS_TABLE = f"{CATALOG}.{SCHEMA_ANALYTICS}.observations"
STATIONS_VIEW = f"{CATALOG}.{SCHEMA_ANALYTICS}.stations"
