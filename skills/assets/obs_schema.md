## Schema
### weather.analytics.observations
|col_name|data_type|comment|
|---|---|---|
|station_id|string|Station ID referenced in the NOAA Global Historical Climatology Network dataset|
|obs_date|date|Date of observation recorded by station|
|AWND|double|Average wind speed|
|PRCP|double|Total daily precipitation|
|SNOW|double|Total daily snowfall|
|SNWD|double|Measured snow depth|
|TMAX|double|Daily maximum (high) temperature|
|TMIN|double|Daily minimum (low) temperature|
|source|string|Source of observed data at station for this date|
|TAVG|double|Daily average temperature (derived)|
|HDD|int|Heating degree days (derived)|
|CDD|int|Cooling degree days (derived)|