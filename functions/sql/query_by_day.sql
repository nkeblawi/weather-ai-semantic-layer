CREATE OR REPLACE FUNCTION weather.analytics.query_by_day(
    p_station_id STRING,
    p_metric STRING,
    p_aggregation STRING,
    p_start_date DATE,
    p_end_date DATE,
    p_threshold_operator STRING DEFAULT NULL,
    p_threshold_value DOUBLE DEFAULT NULL
)
RETURNS STRING
COMMENT '
Answers a unit = day query from the weather query resolver skill against weather.analytics.observations. 

Returns JSON {value, obs_date}

obs_date is populated only when p_aggregation is max or min (the date the extreme occurred), and is null for mean/median/sum/stddev/count, which aggregate across many days with no single date to report.

p_metric is one of TAVG/TMAX/TMIN/PRCP/SNOW/SNWD/AWND/HDD/CDD. 
p_aggregation is one of mean/median/sum/max/min/stdev/count. 
p_threshold_operator (one of >, >=, <, <=, =).
p_threshold_value are required together only when p_aggregation is count.
'
RETURN ((
    SELECT to_json(named_struct(
        'value', COALESCE(
            CASE WHEN p_aggregation = 'mean'   THEN AVG(value) END,
            CASE WHEN p_aggregation = 'median' THEN MEDIAN(value) END,
            CASE WHEN p_aggregation = 'sum'    THEN SUM(value) END,
            CASE WHEN p_aggregation = 'max'    THEN MAX(value) END,
            CASE WHEN p_aggregation = 'min'    THEN MIN(value) END,
            CASE WHEN p_aggregation = 'stddev' THEN STDDEV(value) END,
            CASE WHEN p_aggregation = 'count'  THEN CAST(COUNT(*) AS DOUBLE) END
        ),
        'obs_date',
            CASE
                WHEN p_aggregation = 'max' THEN MAX_BY(obs_date, value)
                WHEN p_aggregation = 'min' THEN MIN_BY(obs_date, value)
            END
    ))
    FROM (
        SELECT obs_date, value
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
        ) base
        WHERE
            p_aggregation != 'count' OR
            (p_threshold_operator = '>'  AND value > p_threshold_value) OR
            (p_threshold_operator = '>=' AND value >= p_threshold_value) OR
            (p_threshold_operator = '<'  AND value < p_threshold_value) OR
            (p_threshold_operator = '<=' AND value <= p_threshold_value) OR
            (p_threshold_operator = '='  AND value = p_threshold_value)
    ) t
));
