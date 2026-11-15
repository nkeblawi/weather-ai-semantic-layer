CREATE OR REPLACE FUNCTION weather.analytics.query_records(
    p_station_id STRING,
    p_metric STRING,
    p_aggregation STRING,
    p_start_date DATE,
    p_end_date DATE,
    p_month_filter ARRAY<INT> DEFAULT NULL
)
RETURNS STRING
COMMENT '
Days in p_start_date..p_end_date that set or tied the daily record for their calendar date, judged against the record as it stood that day (earlier years only, as NWS counts them).

Returns JSON {value, n_tied, rows, rows_truncated, n_days}.

p_aggregation is record_high (the highest value for the date: TMAX, TMIN, PRCP, SNOW) or record_low (the lowest: TMIN). PRCP and SNOW count only days above 0, so a dry day never ties a dry record.

A day counts once its calendar date has at least 30 earlier years with a value, or, before that, if it still holds the record for its date across the whole record through today (no later year beat it). The first years of a record are otherwise all records.

value is how many days set or tied a record, n_tied how many of those only tied it. rows holds up to 15 of them in date order as {obs_date, value, previous_record, previous_record_year, tied}; previous_record is null when the date had no earlier value, and on a tie previous_record_year is the most recent year that held it. rows_truncated is true when more than 15 days counted. n_days is how many days in the range (in p_month_filter months, if given) have a value.
'
RETURN ((
    SELECT to_json(named_struct(
        'value', CAST(COUNT_IF(is_record) AS DOUBLE),
        'n_tied', CAST(COUNT_IF(is_record AND value = prev) AS INT),
        'rows', slice(array_sort(
            collect_list(named_struct(
                'obs_date', obs_date, 'value', value,
                'previous_record', prev, 'previous_record_year', prev_year,
                'tied', value = prev
            )) FILTER (WHERE is_record)
        ), 1, 15),
        'rows_truncated', COUNT_IF(is_record) > 15,
        'n_days', CAST(COUNT_IF(in_range AND value IS NOT NULL) AS INT)
    ))
    FROM (
        SELECT obs_date, value, prev, prev_year, in_range,
            in_range AND value IS NOT NULL
            AND (NOT positive_only OR value > 0)
            -- beat or tied the record as it stood (a date's first value is its first record)
            AND (prev IS NULL OR (high AND value >= prev) OR (NOT high AND value <= prev))
            -- 30-year floor, unless no later year has beaten it either
            AND (prior_years >= 30 OR (high AND value >= whole_record) OR (NOT high AND value <= whole_record))
            AS is_record
        FROM (
            SELECT obs_date, value, in_range, high, positive_only,
                -- earlier years of the same calendar date (one row per year)
                CASE WHEN high
                    THEN MAX(value) OVER (PARTITION BY md ORDER BY obs_date ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
                    ELSE MIN(value) OVER (PARTITION BY md ORDER BY obs_date ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
                END AS prev,
                -- the year that held it; on a tie the most recent one
                CASE WHEN high
                    THEN MAX_BY(YEAR(obs_date), named_struct('v', value, 'y', YEAR(obs_date)))
                        OVER (PARTITION BY md ORDER BY obs_date ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
                    ELSE MIN_BY(YEAR(obs_date), named_struct('v', value, 'y', -YEAR(obs_date)))
                        OVER (PARTITION BY md ORDER BY obs_date ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
                END AS prev_year,
                COUNT(value) OVER (PARTITION BY md ORDER BY obs_date ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prior_years,
                CASE WHEN high THEN MAX(value) OVER (PARTITION BY md) ELSE MIN(value) OVER (PARTITION BY md) END AS whole_record
            FROM (
                -- SQL UDFs can't reference parameters in a window, so they become plain columns here
                SELECT obs_date, value,
                    DATE_FORMAT(obs_date, 'MM-dd') AS md,
                    obs_date BETWEEN p_start_date AND p_end_date AS in_range,
                    p_aggregation = 'record_high' AS high,
                    p_metric IN ('PRCP', 'SNOW') AS positive_only
                -- the whole record through today: earlier years set the bar, later ones decide the floor exception
                FROM weather.analytics.daily_values(
                    p_station_id, p_metric, DATE '1800-01-01', GREATEST(p_end_date, current_date()), p_month_filter
                )
            ) base
        ) w
    ) r
));
