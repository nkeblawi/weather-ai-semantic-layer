CREATE OR REPLACE FUNCTION weather.analytics.query_by_period(
    p_station_id STRING,
    p_metric STRING,
    p_aggregation STRING,
    p_start_date DATE,
    p_end_date DATE,
    p_unit STRING,
    p_period_aggregation STRING,
    p_threshold_operator STRING DEFAULT NULL,
    p_threshold_value DOUBLE DEFAULT NULL,
    p_month_filter ARRAY<INT> DEFAULT NULL
)
RETURNS STRING
COMMENT '
Answers a unit=month or unit = year query from the weather query resolver skill against weather.analytics.observations. Returns JSON {value, bucket_year, bucket_month} where the bucket_year/bucket_month identify the winning bucket and are populated only when p_aggregation is max or min. 

bucket_month is always null when p_unit is year (a year has no single month to report), and both are null for mean/median/sum/stddev/count, which aggregate across many buckets with no single one to report. 

p_unit is "month" or "year". 

p_period_aggregation (mean or sum) collapses each calendar bucket before p_aggregation (mean/median/sum/max/min/stddev/count) is applied across buckets. 

p_threshold_operator/p_threshold_value are required together only when p_aggregation is count. 

p_month_filter is an array of month numbers 1-12, only used when p_unit is month, to restrict which calendar months form buckets.
'
RETURN ((
    SELECT to_json(named_struct(
        'value', COALESCE(
            CASE WHEN p_aggregation = 'mean'   THEN AVG(bucket_value) END,
            CASE WHEN p_aggregation = 'median' THEN MEDIAN(bucket_value) END,
            CASE WHEN p_aggregation = 'sum'    THEN SUM(bucket_value) END,
            CASE WHEN p_aggregation = 'max'    THEN MAX(bucket_value) END,
            CASE WHEN p_aggregation = 'min'    THEN MIN(bucket_value) END,
            CASE WHEN p_aggregation = 'stddev' THEN STDDEV(bucket_value) END,
            CASE WHEN p_aggregation = 'count'  THEN CAST(COUNT(*) AS DOUBLE) END
        ),
        'bucket_year',
            CASE
                WHEN p_aggregation = 'max' THEN MAX_BY(bucket_year, bucket_value)
                WHEN p_aggregation = 'min' THEN MIN_BY(bucket_year, bucket_value)
            END,
        'bucket_month',
            CASE
                WHEN p_aggregation = 'max' THEN MAX_BY(bucket_month, bucket_value)
                WHEN p_aggregation = 'min' THEN MIN_BY(bucket_month, bucket_value)
            END
    ))
    FROM (
        SELECT
            bucket_year,
            CASE WHEN p_unit = 'month' THEN MAX(bucket_month) ELSE NULL END AS bucket_month,
            CASE WHEN p_period_aggregation = 'mean' THEN AVG(value)
                 WHEN p_period_aggregation = 'sum'  THEN SUM(value)
            END AS bucket_value
        FROM (
            SELECT
                YEAR(obs_date) AS bucket_year,
                MONTH(obs_date) AS bucket_month,
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
              AND (p_unit != 'month' OR p_month_filter IS NULL OR array_contains(p_month_filter, MONTH(obs_date)))
        ) days
        GROUP BY
            bucket_year,
            CASE WHEN p_unit = 'month' THEN bucket_month ELSE NULL END
    ) buckets
    WHERE
        p_aggregation != 'count' OR
        (p_threshold_operator = '>'  AND bucket_value > p_threshold_value) OR
        (p_threshold_operator = '>=' AND bucket_value >= p_threshold_value) OR
        (p_threshold_operator = '<'  AND bucket_value < p_threshold_value) OR
        (p_threshold_operator = '<=' AND bucket_value <= p_threshold_value) OR
        (p_threshold_operator = '='  AND bucket_value = p_threshold_value)
));
