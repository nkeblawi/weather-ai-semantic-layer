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
    p_month_filter ARRAY<INT> DEFAULT NULL,
    p_limit INT DEFAULT NULL,
    p_year_start_month INT DEFAULT 1
)
RETURNS STRING
COMMENT '
Answers a unit=month or unit = year query from the weather query resolver skill against weather.analytics.observations. Returns JSON {value, bucket_year, bucket_month, rows, rows_truncated} where the bucket_year/bucket_month identify the winning bucket and are populated only when p_aggregation is max or min. 

bucket_month is always null when p_unit is year (a year has no single month to report), and both are null for mean/median/sum/stddev/count, which aggregate across many buckets with no single one to report. 

p_unit is "month" or "year". 

p_period_aggregation (mean or sum) collapses each calendar bucket before p_aggregation (mean/median/sum/max/min/stddev/count) is applied across buckets. 

p_threshold_operator/p_threshold_value are required together only when p_aggregation is count. 

p_month_filter is an array of month numbers 1-12, only used when p_unit is month, to restrict which calendar months form buckets.

p_aggregation top/bottom ranks buckets by bucket value (highest first / lowest first) with DENSE_RANK, so tied buckets share a rank. rows holds every bucket ranked 1..p_limit as {rank, bucket_year, bucket_month, value, n_days}, ordered by rank then date; value/bucket_year/bucket_month are null. p_limit defaults to 3 and is clamped to 1-15; rows is capped at 15 entries, and rows_truncated is true when ties pushed more than 15 buckets into the ranks. rows is null for every other aggregation.

p_year_start_month (1-12, default 1) is the month a p_unit = year bucket starts in. 1 is the calendar year. 7 is the snow season, July 1 - June 30, labeled by the year it ends in (bucket_year 2010 = Jul 2009 - Jun 2010). Ignored when p_unit is month.

Coverage rule (top/bottom only): a bucket is ranked only if at least 90% of its calendar days have a non-null value (n_days >= 0.9 * days in the month/year). This drops the current partial month/year, a station''s first partial year, and months with long reporting gaps, which would otherwise dominate bottom rankings. max/min are unchanged.
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
            END,
        'rows',
            CASE WHEN p_aggregation IN ('top', 'bottom') THEN
                slice(array_sort(
                    collect_list(named_struct(
                        'rank', CAST(rk AS INT), 'bucket_year', bucket_year,
                        'bucket_month', bucket_month, 'value', bucket_value, 'n_days', CAST(n_days AS INT)
                    )) FILTER (WHERE is_ranking AND rankable AND rk <= rank_limit)
                ), 1, 15)
            END,
        'rows_truncated',
            CASE WHEN p_aggregation IN ('top', 'bottom') THEN
                COUNT(*) FILTER (WHERE is_ranking AND rankable AND rk <= rank_limit) > 15
            END
    ))
    FROM (
        SELECT *,
            -- top/bottom: 1 = highest/lowest among rankable buckets; the rest rank last and are dropped
            DENSE_RANK() OVER (ORDER BY sort_key DESC NULLS LAST) AS rk
        FROM (
            SELECT *,
                -- SQL UDFs can't reference parameters in a window or an aggregate FILTER,
                -- so the ranking inputs are computed here as plain columns
                -- rounded so float noise (53.300000000000004 vs 53.3) doesn't split ties
                ROUND(CASE WHEN NOT rankable THEN NULL WHEN p_aggregation = 'bottom' THEN -bucket_value ELSE bucket_value END, 2) AS sort_key,
                p_aggregation IN ('top', 'bottom') AS is_ranking,
                GREATEST(LEAST(COALESCE(p_limit, 3), 15), 1) AS rank_limit
            FROM (
            SELECT *,
                -- coverage rule: >= 90% of the bucket's calendar days observed
                bucket_value IS NOT NULL AND n_days >= 0.9 * CASE
                    WHEN p_unit = 'month' THEN DAY(LAST_DAY(MAKE_DATE(bucket_year, bucket_month, 1)))
                    -- days in the 12-month bucket, which starts p_year_start_month of the
                    -- prior year when the year doesn't start in January
                    ELSE DATEDIFF(
                        ADD_MONTHS(MAKE_DATE(bucket_year - IF(COALESCE(p_year_start_month, 1) > 1, 1, 0), COALESCE(p_year_start_month, 1), 1), 12),
                        MAKE_DATE(bucket_year - IF(COALESCE(p_year_start_month, 1) > 1, 1, 0), COALESCE(p_year_start_month, 1), 1)
                    )
                END AS rankable
            FROM (
        SELECT
            bucket_year,
            CASE WHEN p_unit = 'month' THEN MAX(bucket_month) ELSE NULL END AS bucket_month,
            CASE WHEN p_period_aggregation = 'mean' THEN AVG(value)
                 WHEN p_period_aggregation = 'sum'  THEN SUM(value)
            END AS bucket_value,
            COUNT(value) AS n_days
        FROM (
            SELECT
                -- year buckets that start mid-year are labeled by the year they end in:
                -- with p_year_start_month 7, Jul 2009 - Jun 2010 is 2010
                CASE WHEN p_unit = 'year' AND p_year_start_month > 1
                     THEN YEAR(ADD_MONTHS(obs_date, 13 - p_year_start_month))
                     ELSE YEAR(obs_date)
                END AS bucket_year,
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
            ) covered
        ) sortable
    ) ranked
    WHERE
        p_aggregation != 'count' OR
        (p_threshold_operator = '>'  AND bucket_value > p_threshold_value) OR
        (p_threshold_operator = '>=' AND bucket_value >= p_threshold_value) OR
        (p_threshold_operator = '<'  AND bucket_value < p_threshold_value) OR
        (p_threshold_operator = '<=' AND bucket_value <= p_threshold_value) OR
        (p_threshold_operator = '='  AND bucket_value = p_threshold_value)
));
