CREATE OR REPLACE FUNCTION weather.analytics.query_by_day(
    p_station_id STRING,
    p_metric STRING,
    p_aggregation STRING,
    p_start_date DATE,
    p_end_date DATE,
    p_threshold_operator STRING DEFAULT NULL,
    p_threshold_value DOUBLE DEFAULT NULL,
    p_limit INT DEFAULT NULL,
    p_month_filter ARRAY<INT> DEFAULT NULL
)
RETURNS STRING
COMMENT '
Answers a unit = day query from the weather query resolver skill against weather.analytics.observations. 

Returns JSON {value, obs_date, rows, rows_truncated, n_days}

obs_date is populated only when p_aggregation is max or min (the date the extreme occurred), and is null for mean/median/sum/stddev/count, which aggregate across many days with no single date to report.

p_metric is one of TAVG/TMAX/TMIN/PRCP/SNOW/SNWD/AWND/HDD/CDD. 
p_aggregation is one of mean/median/sum/max/min/stddev/count/top/bottom. 
p_threshold_operator (one of >, >=, <, <=, =).
p_threshold_value are required together only when p_aggregation is count.
p_month_filter (optional) keeps only days in those calendar months, across every year in the range: "the hottest January day on record" is max with [1] over the whole record.

p_aggregation top/bottom ranks days by value (highest first / lowest first) with DENSE_RANK, so tied days share a rank. rows holds every day ranked 1..p_limit as {rank, obs_date, value}, ordered by rank then date; value/obs_date are null. p_limit defaults to 3 and is clamped to 1-15; rows is capped at 15 entries, and rows_truncated is true when ties pushed more than 15 days into the ranks. rows is null for every other aggregation.

n_days is how many days in the range (in p_month_filter months, if given) have a value, for every aggregation; for count it is the denominator (value of them met the threshold).
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
            CASE WHEN p_aggregation = 'count'  THEN CAST(COUNT_IF(hit = 1) AS DOUBLE) END
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
            END,
        -- days with a value; count's threshold is applied by COUNT_IF, so every day is still here
        'n_days', CAST(COUNT(value) AS INT)
    ))
    FROM (
        SELECT obs_date, value, hit,
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
            FROM weather.analytics.daily_values(
                p_station_id, p_metric, p_start_date, p_end_date,
                p_month_filter, p_threshold_operator, p_threshold_value
            )
        ) base
    ) t
));
