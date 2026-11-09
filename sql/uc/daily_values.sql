CREATE OR REPLACE FUNCTION weather.analytics.daily_values(
    p_station_id STRING,
    p_metric STRING,
    p_start_date DATE,
    p_end_date DATE,
    p_month_filter ARRAY<INT> DEFAULT NULL,
    p_threshold_operator STRING DEFAULT NULL,
    p_threshold_value DOUBLE DEFAULT NULL
)
RETURNS TABLE (obs_date DATE, value DOUBLE, hit INT)
COMMENT '
One station''s daily values of one metric, the shared read behind query_by_day, query_by_event, query_by_period and query_series.

Returns one row per observed day in p_start_date..p_end_date, optionally only in p_month_filter months. value is p_metric (TAVG/TMAX/TMIN/PRCP/SNOW/SNWD/AWND/HDD/CDD). hit is 1 when value meets p_threshold_operator p_threshold_value (one of >, >=, <, <=, =), 0 when it does not or no threshold is given, and null when value is null.
'
RETURN
    SELECT obs_date, value,
        CASE
            WHEN value IS NULL THEN NULL
            WHEN (p_threshold_operator = '>'  AND value > p_threshold_value)
              OR (p_threshold_operator = '>=' AND value >= p_threshold_value)
              OR (p_threshold_operator = '<'  AND value < p_threshold_value)
              OR (p_threshold_operator = '<=' AND value <= p_threshold_value)
              OR (p_threshold_operator = '='  AND value = p_threshold_value)
            THEN 1 ELSE 0
        END AS hit
    FROM (
        SELECT obs_date,
            CASE p_metric
                WHEN 'TAVG' THEN TAVG
                WHEN 'TMAX' THEN TMAX
                WHEN 'TMIN' THEN TMIN
                WHEN 'PRCP' THEN PRCP
                WHEN 'SNOW' THEN SNOW
                WHEN 'SNWD' THEN SNWD
                WHEN 'AWND' THEN AWND
                WHEN 'HDD' THEN HDD
                WHEN 'CDD' THEN CDD
            END AS value
        FROM weather.analytics.observations
        WHERE station_id = p_station_id
          AND obs_date BETWEEN p_start_date AND p_end_date
          AND (p_month_filter IS NULL OR array_contains(p_month_filter, MONTH(obs_date)))
    ) raw;
