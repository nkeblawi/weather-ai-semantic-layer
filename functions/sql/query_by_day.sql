CREATE OR REPLACE FUNCTION weather.analytics.query_by_day(
    p_station_id STRING,
    p_metric STRING,
    p_aggregation STRING,
    p_start_date DATE,
    p_end_date DATE,
    p_threshold_operator STRING DEFAULT NULL,
    p_threshold_value DOUBLE DEFAULT NULL,
    p_limit INT DEFAULT NULL
)
RETURNS STRING
COMMENT '
Answers a unit = day query from the weather query resolver skill against weather.analytics.observations. 

Returns JSON {value, obs_date, rows, rows_truncated}

obs_date is populated only when p_aggregation is max or min (the date the extreme occurred), and is null for mean/median/sum/stddev/count, which aggregate across many days with no single date to report.

p_metric is one of TAVG/TMAX/TMIN/PRCP/SNOW/SNWD/AWND/HDD/CDD. 
p_aggregation is one of mean/median/sum/max/min/stddev/count/top/bottom. 
p_threshold_operator (one of >, >=, <, <=, =).
p_threshold_value are required together only when p_aggregation is count.

p_aggregation top/bottom ranks days by value (highest first / lowest first) with DENSE_RANK, so tied days share a rank. rows holds every day ranked 1..p_limit as {rank, obs_date, value}, ordered by rank then date; value/obs_date are null. p_limit defaults to 3 and is clamped to 1-15; rows is capped at 15 entries, and rows_truncated is true when ties pushed more than 15 days into the ranks. rows is null for every other aggregation.
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
            END,
        'rows',
            CASE WHEN p_aggregation IN ('top', 'bottom') THEN
                slice(array_sort(
                    collect_list(named_struct('rank', CAST(rk AS INT), 'obs_date', obs_date, 'value', value))
                        FILTER (WHERE is_ranking AND value IS NOT NULL AND rk <= rank_limit)
                ), 1, 15)
            END,
        'rows_truncated',
            CASE WHEN p_aggregation IN ('top', 'bottom') THEN
                COUNT(*) FILTER (WHERE is_ranking AND value IS NOT NULL AND rk <= rank_limit) > 15
            END
    ))
    FROM (
        SELECT obs_date, value,
            -- top/bottom: 1 = highest/lowest; NULL values rank last and are dropped
            DENSE_RANK() OVER (ORDER BY sort_key DESC NULLS LAST) AS rk,
            is_ranking, rank_limit
        FROM (
            SELECT *,
                -- SQL UDFs can't reference parameters in a window or an aggregate FILTER,
                -- so the ranking inputs are computed here as plain columns
                -- rounded so float noise (53.300000000000004 vs 53.3) doesn't split ties
                ROUND(CASE WHEN p_aggregation = 'bottom' THEN -value ELSE value END, 2) AS sort_key,
                p_aggregation IN ('top', 'bottom') AS is_ranking,
                GREATEST(LEAST(COALESCE(p_limit, 3), 15), 1) AS rank_limit
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
            ) v
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
