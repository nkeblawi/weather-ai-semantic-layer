# Data Pipeline

`src/wx/` ingests, cleans, and merges historical weather data on Databricks.
Each script runs as a task in one of the
[Databricks Jobs](../README.md#3-databricks-jobs).

## Stations (`stations/`)

Fetches station lists from GHCN and EMSHR datasets, filters them down to WMO
stations, and merges them into a single `station_merged` table with ICAO/IATA
codes and country info attached (from EMSHR for US stations, and
`cleaned.station_codes` / NOAA's country list for the rest).

Station activation (which stations the app can answer questions about) is a
manual, on-demand step (`activate_station.py`) run by the application admin.
A station whose GHCN ID changed can first have its old IDs' history loaded
under the current ID (the `wx_load_station_history` job; see
[station_lineage.md](station_lineage.md)).

## Observations (`obs/`)

Ingests daily observations from GHCN and ACIS, converts each source to a
common schema, merges them, pivots to one row per station/day, and derives
additional variables (e.g. heating/cooling degree days) into a final
analytics table.

Values that failed a NOAA quality check (a GHCN QFLAG) are set to NULL
during conversion, except values verified as real and listed in the
`cleaned.qc_allowlist` table (e.g. SFO's 106°F record high, which NOAA
flags as an outlier). `sql/maintenance/qc_flag_cleanup.sql` applies the same rule
to rows that were loaded earlier.

## Places (`places/`)

Fetches and parses the GeoNames gazetteer into `analytics.places` (see
[Station coverage](#station-coverage) below).

## App tables and shared modules

- `app/`: creates the app's chat tables and applies their retention policy
  ([governance.md](governance.md)).
- `utils/`: config, HTTP, and Spark helpers used by the scripts above.

## Station coverage

44 stations, each the main airport weather station for a major city: 39 in
the US and 5 in Canada. The active list is in `analytics.stations`. Records
start between 1851 (Washington, DC) and 1994 (Denver). The Canadian records and
Washington, DC's combine each station's older GHCN IDs with its current one (see
the lineage doc).
Other towns resolve through `analytics.places`, every place in the world
with 1,000+ people from [GeoNames](https://www.geonames.org) (CC BY 4.0),
loaded by the `wx_places_ingest` job (run by hand). How a place becomes a
station is in the [query layer](query_layer.md#station-lookup).

[station_lineage.md](station_lineage.md) records each station's source,
record start, long gaps, and QC decisions.
