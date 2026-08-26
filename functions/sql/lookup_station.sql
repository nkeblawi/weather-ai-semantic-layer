CREATE OR REPLACE FUNCTION weather.analytics.lookup_station(
    location_text STRING
)
RETURNS STRING
COMMENT 'Resolves free text (city, airport name/alias, ICAO or FAA code) to an active station. Returns JSON {ghcn_id, name, city} or NULL if no match. Call this in Step 2 of the weather-query-resolver skill before setting station_id.'
RETURN ((
  SELECT to_json(named_struct('ghcn_id', s.ghcn_id, 'name', s.name, 'city', s.city))
  FROM weather.analytics.stations s
  LEFT JOIN weather.analytics.station_aliases a ON a.ghcn_id = s.ghcn_id
  WHERE lower(regexp_replace(location_text, '[^a-zA-Z0-9 ]', '')) IN (
    lower(s.icao), lower(s.faa),
    lower(regexp_replace(s.city, '[^a-zA-Z0-9 ]', '')),
    lower(regexp_replace(s.name, '[^a-zA-Z0-9 ]', ''))
  )
  OR lower(regexp_replace(location_text, '[^a-zA-Z0-9 ]', '')) = lower(regexp_replace(a.alias, '[^a-zA-Z0-9 ]', ''))
  LIMIT 1
));
