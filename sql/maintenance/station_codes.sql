-- Station airport codes that EMSHR (the ICAO/FAA source, US-only) doesn't have, e.g. Canadian stations.
-- emshr_stn_merge.py uses these in station_merged.icao / station_merged.iata where EMSHR has no
-- value; analytics.stations exposes icao and iata, and lookup_station matches both.
--
-- This file is the source of truth: add a row to the VALUES list and rerun it (idempotent: MERGE
-- inserts new station_ids and refreshes changed codes/notes). Then rerun wx_station_ingest
-- (or its merge_station_lists task) so station_merged picks the codes up.
--
-- Written against prod (weather.*); for dev, replace "weather.cleaned." with "weather_dev.cleaned.".
-- No semicolons inside string literals (keeps simple statement splitters working).

-- INSTRUCTIONS: Change out the entries to create new ones when adding new non-US stations
-- YOU WILL NEED: GHCN ID, ICAO, IATA, and full airport name

CREATE TABLE IF NOT EXISTS weather.cleaned.station_codes (
  station_id STRING NOT NULL COMMENT 'GHCN station ID (current)',
  icao       STRING COMMENT 'ICAO airport code, e.g. CYUL',
  iata       STRING COMMENT 'IATA airport code, e.g. YUL',
  note       STRING NOT NULL COMMENT 'Airport name and source',
  added_at   TIMESTAMP NOT NULL
)
COMMENT 'Airport codes for stations EMSHR does not cover, used by emshr_stn_merge.py';

MERGE INTO weather.cleaned.station_codes t
USING (
  SELECT * FROM VALUES
    ('CAN07025251', 'CYUL', 'YUL', 'Montreal-Pierre Elliott Trudeau International Airport'),
    ('CAN06158731', 'CYYZ', 'YYZ', 'Toronto Pearson International Airport'),
    ('CAN08202251', 'CYHZ', 'YHZ', 'Halifax Stanfield International Airport'),
    ('CAN08403505', 'CYYT', 'YYT', 'St. Johns International Airport'),
    ('CAN03012216', 'CYEG', 'YEG', 'Edmonton International Airport')
  AS v(station_id, icao, iata, note)
) s
ON t.station_id = s.station_id
WHEN MATCHED AND (NOT (t.icao <=> s.icao) OR NOT (t.iata <=> s.iata) OR t.note <> s.note) THEN
  UPDATE SET icao = s.icao, iata = s.iata, note = s.note
WHEN NOT MATCHED THEN INSERT (station_id, icao, iata, note, added_at)
  VALUES (s.station_id, s.icao, s.iata, s.note, current_timestamp());
