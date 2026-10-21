# Station lineage

Where each active station's data comes from, what was done to it, and what to watch for. Update
this file whenever a station is activated, spliced, relabeled, or has values allowlisted or blanked.

Last reviewed: 2026-10-21 (43 active stations, prod catalog `weather`).

## How to read this

- **station_id** is the GHCN-Daily ID. It is the key everywhere: `raw.obs_ghcn` through
  `analytics.observations`, the query functions, and the app. `analytics.stations` lists the active
  ones (`station_merged.selected = true` with a WMO ID).
- **Sources:** GHCN-Daily for the full history (`all/<station_id>.dly` at NOAA), plus ACIS for the
  last 1-2 days until GHCN catches up. GHCN replaces ACIS once it has the date.
- **QC:** values with a GHCN quality flag (QFLAG) are set to NULL in `obs_ghcn_conv` and downstream,
  except keys in `cleaned.qc_allowlist`. "QC blanked" below counts those values per station
  (`obs_merged` rows with a QFLAG and a NULL value). Raw values stay in `raw.obs_ghcn`.
- **Single source / spliced:** a *single source* station's rows all come from its own GHCN ID. A
  *spliced* station also holds rows loaded from older IDs under its ID (see
  [Spliced stations](#spliced-stations)).
- **ICAO** is blank for Canadian stations: the ICAO/FAA source (EMSHR) is US-only; their airport
  codes are covered by aliases instead.
- **Country** is `station_merged.country_code` (FIPS codes: US, CA = Canada).
- **Record start** is the first date in `analytics.observations`. Long gaps are runs of 28+ missing
  days.

## Active stations

| City | Country | station_id | ICAO | WMO | Record start | Activated | Source | QC blanked |
|---|---|---|---|---|---|---|---|---|
| Anchorage | US | USW00026451 | PANC | 70273 | 1952-04-01 | 2026-10-20 | single | 14 |
| Atlanta | US | USW00013874 | KATL | 72219 | 1930-01-01 | 2026-09-15 | single | 4 |
| Baltimore | US | USW00093721 | KBWI | 72406 | 1939-07-01 | 2026-10-20 | single | 39 |
| Boston | US | USW00014739 | KBOS | 72509 | 1936-01-01 | 2026-10-20 | single | 32 |
| Charlotte | US | USW00013881 | KCLT | 72314 | 1939-07-01 | 2026-10-20 | single | 4 |
| Chicago | US | USW00094846 | KORD | 72530 | 1958-11-01 | 2026-09-15 | single | 12 |
| Cincinnati, OH | US | USW00093814 | KCVG | 72421 | 1948-01-01 | 2026-10-20 | single | 6 |
| Cleveland | US | USW00014820 | KCLE | 72524 | 1938-05-01 | 2026-10-20 | single | 26 |
| Columbus | US | USW00014821 | KCMH | 72428 | 1948-01-01 | 2026-10-20 | single | 0 |
| Dallas | US | USW00003927 | KDFW | 72259 | 1953-04-01 | 2026-10-20 | single | 1 |
| Denver | US | USW00003017 | KDEN | 72565 | 1994-07-01 | 2026-10-20 | single | 698 |
| Detroit | US | USW00094847 | KDTW | 72537 | 1958-12-01 | 2026-10-20 | single | 0 |
| Edmonton | CA | CAN03012216 | | 71123 | 1959-05-08 | 2026-10-21 | spliced | 71 |
| Halifax | CA | CAN08202251 | | 71395 | 1953-01-21 | 2026-10-21 | spliced | 55 |
| Honolulu | US | USW00022521 | PHNL | 91182 | 1940-05-01 | 2026-10-20 | single | 1 |
| Houston | US | USW00012960 | KIAH | 72243 | 1969-06-01 | 2026-10-20 | single | 1 |
| Indianapolis | US | USW00093819 | KIND | 72438 | 1948-01-01 | 2026-10-20 | single | 1 |
| Kansas City | US | USW00003947 | KMCI | 72446 | 1972-10-01 | 2026-10-20 | single | 9 |
| Las Vegas | US | USW00023169 | KLAS | 72386 | 1948-09-01 | 2026-10-20 | single | 0 |
| Los Angeles | US | USW00023174 | KLAX | 72295 | 1944-08-01 | 2026-09-15 | single | 4 |
| Miami | US | USW00012839 | KMIA | 72202 | 1948-01-01 | 2026-09-15 | single | 0 |
| Minneapolis | US | USW00014922 | KMSP | 72658 | 1938-04-01 | 2026-10-20 | single | 2 |
| Montreal | CA | CAN07025251 | | 71627 | 1941-09-01 | 2026-10-21 | spliced | 29 |
| Nashville | US | USW00013897 | KBNA | 72327 | 1948-01-01 | 2026-10-20 | single | 6 |
| New Orleans | US | USW00012916 | KMSY | 72231 | 1948-01-01 | 2026-10-20 | single | 2 |
| New York | US | USW00014732 | KLGA | 72503 | 1939-10-01 | 2026-09-09 | single | 6 |
| Orlando | US | USW00012815 | KMCO | 72205 | 1952-05-01 | 2026-10-20 | single | 14 |
| Philadelphia | US | USW00013739 | KPHL | 72408 | 1940-07-01 | 2026-10-20 | single | 1 |
| Phoenix | US | USW00023183 | KPHX | 72278 | 1933-06-01 | 2026-10-20 | single | 24 |
| Pittsburgh | US | USW00094823 | KPIT | 72520 | 1948-01-01 | 2026-10-20 | single | 18 |
| Portland | US | USW00024229 | KPDX | 72698 | 1938-04-01 | 2026-10-20 | single | 1 |
| Raleigh | US | USW00013722 | KRDU | 72306 | 1944-05-01 | 2026-10-20 | single | 8 |
| Salt Lake City | US | USW00024127 | KSLC | 72572 | 1948-01-01 | 2026-10-20 | single | 3 |
| San Antonio | US | USW00012921 | KSAT | 72253 | 1946-09-01 | 2026-10-20 | single | 3 |
| San Diego | US | USW00023188 | KSAN | 72290 | 1939-07-01 | 2026-10-20 | single | 3 |
| San Francisco | US | USW00023234 | KSFO | 72494 | 1945-07-01 | 2026-09-15 | single | 4 |
| Seattle | US | USW00024233 | KSEA | 72793 | 1948-01-01 | 2026-10-20 | single | 9 |
| St. John's | CA | CAN08403505 | | 71801 | 1942-01-01 | 2026-10-21 | spliced | 44 |
| St. Louis | US | USW00013994 | KSTL | 72434 | 1938-04-01 | 2026-10-20 | single | 0 |
| Sterling | US | USW00093738 | KIAD | 72403 | 1960-04-01 | 2026-08-25 | single | 1 |
| Tampa | US | USW00012842 | KTPA | 72211 | 1939-02-01 | 2026-10-20 | single | 0 |
| Toronto | CA | CAN06158731 | | 71624 | 1937-11-01 | 2026-10-21 | spliced | 16 |
| Washington, DC | US | USW00013743 | KDCA | 72405 | 1941-06-01 | 2026-09-09 | single | 0 |

Activated dates are UTC, from the `wx_activate_station` run history; the 30-station expansion runs
are in `logs/activate_runs_prod.tsv`. Washington, DC's station sits in Virginia (Reagan National);
Cincinnati's in Kentucky (CVG). Both cities are labeled with their own state ("Washington, DC",
"Cincinnati, OH") so the app doesn't append the station's state (VA, KY); every other city is
labeled bare and the app shows it with its state ("Portland, OR").

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

A spliced station's record joins one or more older GHCN IDs to the current ID, for stations whose
ID changed (renumbering, automation) or whose airport moved. Used for the Canadian stations
(tranche 7 in `logs/station_expansion_plan.md`).

**Method (one-time history load):** old IDs are downloaded once and written to `raw.obs_ghcn`
**under the current ID**, then the current ID is activated as usual
(`src/wx/stations/load_station_history.py`, run by the `wx_load_station_history` job with
`station_id`, `history_ids`, and `dry_run`, which defaults to true). An old ID's value is loaded for every (date,
variable) the current ID has no value for; where both have one, the current ID wins. GHCN ingest
never lets a missing day overwrite an existing value (`keep_existing_values=True` in
`merge_into_target`), so a later `--full` re-ingest can't erase the history. After the load, rows no
longer show which ID they came from, so this file is the record of it.

**For each spliced station, record:** current and old IDs and the years each supplied; rows loaded
per old ID and variable (loader output); load date and run IDs; overlap check (mean TMAX/TMIN
difference on days both IDs report; a large difference suggests a site move); the handoff and any
days neither ID reported.

### Montreal (CAN07025251)

| Field | Value |
|---|---|
| Current ID | CAN07025251, MONTREAL INTL A (Trudeau, YUL), WMO 71627; reports from 2013-02-18 |
| Old ID | CAN07025250, MONTREAL/PIERRE ELLIOTT TRUDEAU, same WMO; 1941-09-01 to 2013-02-13 |
| Rows loaded | 125,217: PRCP 26,056; SNOW 26,051; SNWD 21,105; TMAX 26,015; TMIN 25,990 |
| Loaded | 2026-10-21 UTC, prod; loader run 764668918848583, activation run 482111423610793 |
| Overlap check | no day where both IDs report (clean handoff) |
| Handoff | 2013-02-14 to 2013-02-17 empty: neither ID reported |
| Result | 1941-09-01 onward; TMAX on 30,923 days; gaps over 4 days only 1942-03 (6 + 5 days) and 2013-03-14 to 03-19 (6 days, current ID) |
| QC | 29 values blanked (SNWD 18, SNOW 9, TMIN 2), none beyond a station extreme; no allowlist entries |
| Extremes | 100°F (1975-08-01), -36°F (1957-01-15), Montreal's known records |

### Toronto (CAN06158731)

| Field | Value |
|---|---|
| Current ID | CAN06158731, TORONTO INTL A (Pearson, YYZ), WMO 71624; reports from 2013-06-13 |
| Old ID | CAN06158733, TORONTO LESTER B. PEARSON INT'L A, same WMO; 1937-11-01 to 2013-06-13 |
| Rows loaded | 131,000: PRCP 27,429; SNOW 27,433; SNWD 21,288; TMAX 27,428; TMIN 27,422 |
| Loaded | 2026-10-21 UTC, prod; loader run 866317441639305, activation run 1114849833457944 |
| Overlap check | 1 day (2013-06-13): TMAX 0.5°F, TMIN 0.7°F apart, same PRCP; current ID kept |
| Handoff | none missing |
| Result | 1937-11-01 onward; TMAX on 32,273 days; only gap over 6 days: Sep-Dec 1939 (122 days, source) |
| QC | 16 values blanked, none beyond a station extreme |
| Extremes | 101°F, -24°F |

### Halifax (CAN08202251)

| Field | Value |
|---|---|
| Current ID | CAN08202251, HALIFAX STANFIELD INT'L A (YHZ), WMO 71395; reports from 2012-09-15 |
| Old ID | CAN08202250, same name and WMO; 1953-01-21 to 2012-09-12 |
| Rows loaded | 96,649: PRCP 19,439; SNOW 19,452; SNWD 18,830; TMAX 19,465; TMIN 19,463 |
| Loaded | 2026-10-21 UTC, prod; loader run 119445429667977, activation run 1057365700960034 |
| Overlap check | none (clean handoff) |
| Handoff | 2012-09-13 to 09-14 empty: neither ID reported |
| Result | **No data 1954-03-01 to 1960-06-30** (2,314 days, missing from the old ID at NOAA); continuous from 1960-07 |
| QC | 55 values blanked, none beyond a station extreme |
| Extremes | 95°F, -19°F; snowiest day 26.0" |

### St. John's (CAN08403505)

| Field | Value |
|---|---|
| Current ID | CAN08403505, ST. JOHN'S INTL A (YYT), WMO 71801; reports from 2012-03-20 |
| Old ID | CAN08403506, ST JOHN'S A, same WMO; 1942-01-01 to 2012-03-19 |
| Rows loaded | 123,416: PRCP 25,646; SNOW 25,646; SNWD 20,836; TMAX 25,644; TMIN 25,644 |
| Loaded | 2026-10-21 UTC, prod; loader run 340569447084399, activation run 1020840551829492 |
| Overlap check | none (clean handoff) |
| Handoff | none missing; the current ID has its own 14-day gap 2012-04-07 to 04-20 |
| Result | 1942-01-01 onward; TMAX on 30,844 days |
| QC | 44 values blanked, none beyond a station extreme |
| Extremes | 89°F, -11°F; snowiest day 30.0" (Jan 2020 storm) |

### Edmonton (CAN03012216)

| Field | Value |
|---|---|
| Current ID | CAN03012216, EDMONTON INTL A (YEG), WMO 71123; reports from 2012-04-12 |
| Old ID | CAN03012205, EDMONTON INT'L A, same WMO; 1959-05-08 to 2012-04-11 |
| Rows loaded | 93,945: PRCP 18,805; SNOW 18,814; SNWD 18,729; TMAX 18,798; TMIN 18,799 |
| Loaded | 2026-10-21 UTC, prod; loader run 823773307222817, activation run 123577734972740 |
| Overlap check | none (clean handoff) |
| Handoff | none missing; the current ID has its own 10-day gap 2012-10-23 to 11-01 |
| Result | **No data Jul 1959 and 1959-09-01 to 1960-11-13** (source); continuous from 1960-11 |
| QC | 71 values blanked, none beyond a station extreme |
| Extremes | 96°F, -55°F |

**Cautions:**
- Never activate or ingest an old ID on its own; its history already lives under the current ID.
- Re-running the loader for a station is safe (same keys and values). A `--full` re-ingest of the
  current ID is safe (missing days don't overwrite loaded values; days the current ID has replace
  the old ID's, which is intended).
- Converted rows don't pick up later edits (issue #8 in `logs/issues.txt`). To redo a station,
  delete its rows from `obs_ghcn_conv`, `obs_merged`, `obs_pivot` and `observations`, then re-run the
  loader and the activation job.
- QC allowlist entries for spliced years use the current ID.

## Planned (not active)

None. Tranche 7 (Montreal, Toronto, Halifax, St. John's, Edmonton) was activated on 2026-10-21.
