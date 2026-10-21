# Station lineage

Where each active station's data comes from, what was done to it, and what to watch for. Update
this file whenever a station is activated, spliced, relabeled, or has values allowlisted or blanked.

Last reviewed: 2026-10-20 (38 active stations, prod catalog `weather`).

## How to read this

- **station_id** is the GHCN-Daily ID. It is the key everywhere: `raw.obs_ghcn` through
  `analytics.observations`, the query functions, and the app. `analytics.stations` lists the active
  ones (`station_merged.selected = true` with a WMO ID).
- **Sources:** GHCN-Daily for the full history (`all/<station_id>.dly` at NOAA), plus ACIS for the
  last 1-2 days until GHCN catches up. GHCN replaces ACIS once it has the date.
- **QC:** values with a GHCN quality flag (QFLAG) are set to NULL in `obs_ghcn_conv` and downstream,
  except keys in `cleaned.qc_allowlist`. "QC blanked" below counts those values per station
  (`obs_merged` rows with a QFLAG and a NULL value). Raw values stay in `raw.obs_ghcn`.
- **Single source / spliced:** every station below is *single source*: all its rows come from its
  own GHCN ID. A *spliced* station would also hold rows loaded from older IDs under its ID (see
  [Spliced stations](#spliced-stations)); there are none yet.
- **Record start** is the first date in `analytics.observations`. Long gaps are runs of 28+ missing
  days.

## Active stations

| City | station_id | ICAO | WMO | Record start | Activated | Source | QC blanked |
|---|---|---|---|---|---|---|---|
| Anchorage | USW00026451 | PANC | 70273 | 1952-04-01 | 2026-10-20 | single | 14 |
| Atlanta | USW00013874 | KATL | 72219 | 1930-01-01 | 2026-09-15 | single | 4 |
| Baltimore | USW00093721 | KBWI | 72406 | 1939-07-01 | 2026-10-20 | single | 39 |
| Boston | USW00014739 | KBOS | 72509 | 1936-01-01 | 2026-10-20 | single | 32 |
| Charlotte | USW00013881 | KCLT | 72314 | 1939-07-01 | 2026-10-20 | single | 4 |
| Chicago | USW00094846 | KORD | 72530 | 1958-11-01 | 2026-09-15 | single | 12 |
| Cincinnati | USW00093814 | KCVG | 72421 | 1948-01-01 | 2026-10-20 | single | 6 |
| Cleveland | USW00014820 | KCLE | 72524 | 1938-05-01 | 2026-10-20 | single | 26 |
| Columbus | USW00014821 | KCMH | 72428 | 1948-01-01 | 2026-10-20 | single | 0 |
| Dallas | USW00003927 | KDFW | 72259 | 1953-04-01 | 2026-10-20 | single | 1 |
| Denver | USW00003017 | KDEN | 72565 | 1994-07-01 | 2026-10-20 | single | 698 |
| Detroit | USW00094847 | KDTW | 72537 | 1958-12-01 | 2026-10-20 | single | 0 |
| Honolulu | USW00022521 | PHNL | 91182 | 1940-05-01 | 2026-10-20 | single | 1 |
| Houston | USW00012960 | KIAH | 72243 | 1969-06-01 | 2026-10-20 | single | 1 |
| Indianapolis | USW00093819 | KIND | 72438 | 1948-01-01 | 2026-10-20 | single | 1 |
| Kansas City | USW00003947 | KMCI | 72446 | 1972-10-01 | 2026-10-20 | single | 9 |
| Las Vegas | USW00023169 | KLAS | 72386 | 1948-09-01 | 2026-10-20 | single | 0 |
| Los Angeles | USW00023174 | KLAX | 72295 | 1944-08-01 | 2026-09-15 | single | 4 |
| Miami | USW00012839 | KMIA | 72202 | 1948-01-01 | 2026-09-15 | single | 0 |
| Minneapolis | USW00014922 | KMSP | 72658 | 1938-04-01 | 2026-10-20 | single | 2 |
| Nashville | USW00013897 | KBNA | 72327 | 1948-01-01 | 2026-10-20 | single | 6 |
| New Orleans | USW00012916 | KMSY | 72231 | 1948-01-01 | 2026-10-20 | single | 2 |
| New York | USW00014732 | KLGA | 72503 | 1939-10-01 | 2026-09-09 | single | 6 |
| Orlando | USW00012815 | KMCO | 72205 | 1952-05-01 | 2026-10-20 | single | 14 |
| Philadelphia | USW00013739 | KPHL | 72408 | 1940-07-01 | 2026-10-20 | single | 1 |
| Phoenix | USW00023183 | KPHX | 72278 | 1933-06-01 | 2026-10-20 | single | 24 |
| Pittsburgh | USW00094823 | KPIT | 72520 | 1948-01-01 | 2026-10-20 | single | 18 |
| Portland | USW00024229 | KPDX | 72698 | 1938-04-01 | 2026-10-20 | single | 1 |
| Raleigh | USW00013722 | KRDU | 72306 | 1944-05-01 | 2026-10-20 | single | 8 |
| Salt Lake City | USW00024127 | KSLC | 72572 | 1948-01-01 | 2026-10-20 | single | 3 |
| San Antonio | USW00012921 | KSAT | 72253 | 1946-09-01 | 2026-10-20 | single | 3 |
| San Diego | USW00023188 | KSAN | 72290 | 1939-07-01 | 2026-10-20 | single | 3 |
| San Francisco | USW00023234 | KSFO | 72494 | 1945-07-01 | 2026-09-15 | single | 4 |
| Seattle | USW00024233 | KSEA | 72793 | 1948-01-01 | 2026-10-20 | single | 9 |
| St. Louis | USW00013994 | KSTL | 72434 | 1938-04-01 | 2026-10-20 | single | 0 |
| Sterling | USW00093738 | KIAD | 72403 | 1960-04-01 | 2026-08-25 | single | 1 |
| Tampa | USW00012842 | KTPA | 72211 | 1939-02-01 | 2026-10-20 | single | 0 |
| Washington, DC | USW00013743 | KDCA | 72405 | 1941-06-01 | 2026-09-09 | single | 0 |

Activated dates are UTC, from the `wx_activate_station` run history; the 30-station expansion runs
are in `logs/activate_runs_prod.tsv`. Washington, DC's station sits in Virginia (Reagan National);
Cincinnati's in Kentucky (CVG).

## Station notes

Only stations with something to know. Anything not listed has a continuous record from its start.

**Records shorter than the city's history.** These use today's main airport; an older airport or
downtown station holds earlier years and is not included. Candidates for a future history load
(below), if the sites are judged comparable:
- **Denver** (from 1994; DIA opened 1995). Earlier: Stapleton, USW00023062 (in station_merged).
- **Kansas City** (from 1972, MCI). Earlier: downtown airport (KMKC), not in station_merged.
- **Dallas** (from 1953, DFW site). Earlier: Love Field, not in station_merged.
- **Houston** (from 1969, IAH). Earlier: Hobby, USW00012918 (in station_merged).
- **Chicago** (from 1958, O'Hare). Earlier: Midway, USW00014819 (in station_merged).
- **Sterling / Dulles** (from 1960, airport opened 1962).

**Long gaps** (missing from the source, not from the pipeline):
- **Charlotte:** 1942-01-01 to 1943-12-31 (730 days).
- **Orlando:** 1971-01-01 to 1972-12-31 (731 days).
- **Philadelphia:** 1945-07-01 to 1945-11-30 (153 days).
- **Phoenix:** seven gaps 1935-1947 (about 1,860 days in all); continuous from Dec 1947.
- **Honolulu:** June 1942 (30 days), Jul-Sep 1949 (92 days).
- **Anchorage:** Dec 1952 to Jan 1953 (62 days).
- **Denver:** Sep 1994 to Jan 1995 (153 days), Oct 2013 (31 days).
- **Sterling:** Aug 1960 to Nov 1961 (487 days), Oct 1962 (31 days).

**QC decisions:**
- **San Francisco, 1961-06-14 TMAX 106°F:** kept via `cleaned.qc_allowlist` (NOAA flags it O,
  outlier; it is SFO's official all-time record).
- **Nashville, 1956-10-08 TMIN -40°F:** blanked (flag G; the day's high was 78°F). Record low is
  now -17°F (Jan 1985), matching the official record.
- **Denver:** 698 values blanked, almost all SNOW/SNWD in 1995-2005 (flag Z, rejected by NOAA after
  manual review). GHCN also has no SNOW at all for 1998-2005, so **snowfall is usable from 2006 only**.

**Unverified values:**
- **Salt Lake City, 2026-07-12 TMAX 109°F:** new all-time high (previous 107°F), not flagged by
  NOAA, from a preliminary source (`sflag = 1`). Check against NWS Salt Lake City records.

**Data sources and timing:**
- **Los Angeles, San Francisco:** the 10:00 UTC ACIS ingest runs before West Coast daily summaries
  exist, so ACIS returns nothing; these two show GHCN only (2-3 days behind). See issues log.
- **Sterling** is labeled by town, not by airport ("Sterling", for Dulles); the other original
  stations use the city.

## Spliced stations

None yet. A spliced station is one whose record joins one or more older GHCN IDs to the current ID,
for stations whose ID changed (renumbering, automation) or whose airport moved. Planned for the
Canadian stations (tranche 7 in `logs/station_expansion_plan.md`).

**Method (one-time history load):** old IDs are downloaded once and written to `raw.obs_ghcn`
**under the current ID**, then the current ID is activated as usual. Only (variable, month) pairs
the current ID's `.dly` file has no line for are loaded, so the current ID's data always wins and a
later `--full` re-ingest can't overwrite the history. After the load, rows no longer show which ID
they came from, so this file is the record of it.

**For each spliced station, record:**

| Field | Example |
|---|---|
| City / station_id (current) | Montreal / CAN07025251 |
| Old IDs and the years each supplied | CAN07025250: 1941-2013 |
| Variables and months filled | all variables before the current ID's first line per variable |
| Rows loaded per old ID and variable | (from the loader output) |
| Date loaded, by whom | |
| Overlap check | mean TMAX/TMIN difference on overlapping days; site moved? |

**Cautions:**
- Never activate or ingest an old ID on its own; its history already lives under the current ID.
- Re-running the loader for a station is safe (same keys and values). A `--full` re-ingest of the
  current ID is safe (it only writes months the loader skipped).
- Converted rows don't pick up later edits (issue #8 in `logs/issues.txt`). To redo a station,
  delete its rows from `obs_ghcn_conv`, `obs_merged`, `obs_pivot` and `observations`, then re-run the
  loader and the activation job.
- QC allowlist entries for spliced years use the current ID.

## Planned (not active)

Tranche 7, Canadian airports (plan only; see `logs/station_expansion_plan.md`):

| City | Current ID | Old IDs | Expected record |
|---|---|---|---|
| Montreal | CAN07025251 | CAN07025250 | 1941+ |
| Toronto | CAN06158731 | CAN06158733 | 1937+ |
| Vancouver | CAN01108395 | CAN01108447 | 1937+ |
| Halifax | CAN08202251 | CAN08202250 | 1953+ |
| St. John's | CAN08403505 | CAN08403506 | 1942+ |
| Edmonton | CAN03012216 | CAN03012205 | 1959+ |
