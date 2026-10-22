"""
Metric units and labels, and the conversion of answer values to the
query's output unit.
"""

# Physical unit each metric is stored in (the pipeline converts to imperial).
METRIC_UNITS = {
    "TAVG": "°F",
    "TMAX": "°F",
    "TMIN": "°F",
    "PRCP": "inches",
    "SNOW": "inches",
    "SNWD": "inches",
    "AWND": "mph",
    "HDD": "heating degree days",
    "CDD": "cooling degree days",
}
# When aggregation is `count`, the value counts analysis-grain things, not
# the metric -- so the unit is the plural grain word.
COUNT_UNITS = {
    "day": "days",
    "event": "events",
    "month": "months",
    "year": "years",
    "snow_season": "snow seasons",
    "season": "seasons",
}

TEMP_METRICS = {"TAVG", "TMAX", "TMIN"}

# Display names for the ranking table header.
METRIC_LABELS = {
    "TAVG": "Average temperature",
    "TMAX": "High temperature",
    "TMIN": "Low temperature",
    "PRCP": "Precipitation",
    "SNOW": "Snowfall",
    "SNWD": "Snow depth",
    "AWND": "Average wind speed",
    "HDD": "Heating degree days",
    "CDD": "Cooling degree days",
}


def answer_units(entry: dict) -> str:
    """Physical unit the answer value should be stated in."""
    grain = entry.get("unit", "day")
    if entry["aggregation"] == "count":
        return COUNT_UNITS.get(grain, "days")
    if grain == "event" and entry.get("event_value") == "duration":
        return "days"
    if entry.get("output_unit") == "C" and entry["metric"] in TEMP_METRICS:
        return "°C"
    return METRIC_UNITS.get(entry["metric"], "")


def convert_value(entry: dict, value):
    """
    Apply the query's output_unit to the raw value. Only Fahrenheit ->
    Celsius, and only for temperature metrics. A stddev is a spread, so it
    scales by 5/9 without the 32-degree offset; everything else passes
    through unchanged.
    """
    if value is None:
        return None
    if entry.get("output_unit") == "C" and entry["metric"] in TEMP_METRICS:
        if entry["aggregation"] == "stddev":
            return value * 5 / 9
        return (value - 32) * 5 / 9
    return value
