## Schema
### weather.analytics.stations
|col_name|data_type|comment|
|---|---|---|
|ghcn_id|string|GHCN station ID; the value used as `station_id` when querying weather.analytics.observations|
|icao|string|ICAO airport code|
|iata|string|IATA airport code (e.g. IAD, YUL)|
|name|string|Raw station name|
|city|string|City, set manually at station activation time|
|state|string|State or province code|
|country_name|string|Country name|
|country_code|string|Country code|
|wmo|string|WMO station ID|
|latitude|double|Station latitude|
|longitude|double|Station longitude|
|elevation|double|Station elevation|
