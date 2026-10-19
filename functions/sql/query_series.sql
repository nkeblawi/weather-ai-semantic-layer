CREATE OR REPLACE FUNCTION weather.analytics.query_series(
    p_station_id STRING,
    p_metric STRING,
    p_start_date DATE,
    p_end_date DATE,
    p_unit STRING,
    p_period_aggregation STRING,
    p_month_filter ARRAY<INT> DEFAULT NULL,
    p_year_start_month INT DEFAULT 1,
    p_threshold_operator STRING DEFAULT NULL,
    p_threshold_value DOUBLE DEFAULT NULL
)
RETURNS STRING
COMMENT '
Answers a p_aggregation = series query (a /plot request) from the weather query resolver skill against weather.analytics.observations. Called only by answer_query.

Returns every bucket as JSON columns (parallel arrays, ordered by date) rather than one value, so a chart can be drawn from it.

p_unit:
- month: one point per calendar month. Returns {years, months, values, n_days}.
- year: one point per year. p_year_start_month shifts the year (7 = snow season Jul-Jun; 12 with p_month_filter [12, 1, 2] = winter, Dec-Feb kept together), labeled by the year it ends in. p_month_filter keeps only those months in each year, which is how a season (e.g. summer = [6, 7, 8]) becomes one point per year. Returns {years, values, n_days}.
- month_of_year: monthly climatology, 12 points. Each month (year, month) is collapsed first, then averaged across years. Returns {months, values, stddev, n_years}; stddev is the spread of the monthly values across years.
- season_of_year: seasonal climatology, 4 points (winter DJF, spring MAM, summer JJA, fall SON). Each season (year, season) is collapsed first, then averaged across years; a winter takes December from the year before (winter 1991 = Dec 1990 - Feb 1991). Returns {seasons, values, stddev, n_years}.
- week_of_year: weekly climatology, 52 points (7-day weeks from Jan 1; week 52 runs to Dec 31). Returns {weeks, values, stddev, n_years}; a week counts with 6 of its days observed.
- value: a histogram of daily values -- how many days had each rounded value (whole degrees; 0.01 inch for PRCP/SNOW/SNWD), optionally only in p_month_filter months. Returns {values, counts}.
- day_of_year: daily climatology and records, 366 points (Feb 29 from leap years only). Each calendar day collects its values across the years in the range. Returns {days ("MM-DD"), values (mean), stddev, n_years, max, max_year, min, min_year}; on a tie the most recent year is the record year. p_period_aggregation is ignored. Normals are this over a baseline range (smoothed by the caller); records are this over the whole record.
- day: one point per day, for a span of 366 days or less (daily points are never plotted over longer spans). Returns {dates, values}, skipping days with no value; p_period_aggregation is ignored. A longer span returns {"error": "span_too_long"}.

first_date / last_date (unit year, with p_year_start_month and p_month_filter as for year buckets): the first and last day each year meeting the threshold, e.g. the last spring freeze (TMIN <= 32, months 1-6) or the first measurable snow of a Jul-Jun season. Returns {years, first, last, n_days}; a year with no such day has nulls.

p_period_aggregation collapses each month/year/season/week (and, for any, each calendar day) from its daily values: mean, sum, any (1 if at least one day meets the threshold, else 0 -- averaged across years by a climatology, that is the share of years), or count (the number of days meeting p_threshold_operator p_threshold_value, one of >, >=, <, <=, =; e.g. days >= 90). In a climatology, count averages the per-year counts (e.g. the average number of 90-degree days in July).

Coverage rule: a month, year, or season counts only if at least 90% of its calendar days have a non-null value (for a filtered year, counting only the p_month_filter months), so partial periods never enter a series or a climatology. n_days / n_years let the caller apply its own completeness checks (e.g. a 30-year normal needing at least 24 years).

Values are rounded: 2 decimals for PRCP/SNOW/SNWD, 1 for everything else.
'
RETURN (
    CASE
        WHEN p_unit = 'year' AND p_period_aggregation IN ('first_date', 'last_date') THEN (
            SELECT to_json(named_struct(
                'years', transform(pts, p -> p.y),
                'first', transform(pts, p -> p.f),
                'last', transform(pts, p -> p.l),
                'n_days', transform(pts, p -> p.n)
            ))
            FROM (
                SELECT array_sort(collect_list(named_struct('y', bucket_year, 'f', first_hit, 'l', last_hit, 'n', n_days))) AS pts
                FROM (
                    SELECT
                        bucket_year,
                        MIN(IF(hit = 1, obs_date, NULL)) AS first_hit,
                        MAX(IF(hit = 1, obs_date, NULL)) AS last_hit,
                        CAST(COUNT(value) AS INT) AS n_days,
                        MAKE_DATE(bucket_year - IF(COALESCE(p_year_start_month, 1) > 1, 1, 0), COALESCE(p_year_start_month, 1), 1) AS bucket_start
                    FROM (
                        -- hit computed below the aggregate: aggregates can't reference parameters
                        SELECT *, CASE
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
                            CASE WHEN COALESCE(p_year_start_month, 1) > 1
                                 THEN YEAR(ADD_MONTHS(obs_date, 13 - p_year_start_month)) ELSE YEAR(obs_date)
                            END AS bucket_year,
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
                        ) raw
                    ) days
                    GROUP BY bucket_year
                ) buckets
                -- coverage rule, as for year buckets: >= 90% of the window's days observed
                WHERE n_days >= 0.9 * AGGREGATE(
                    SEQUENCE(0, 11), 0,
                    (acc, i) -> acc + IF(
                        p_month_filter IS NULL OR array_contains(p_month_filter, MONTH(ADD_MONTHS(bucket_start, i))),
                        DAY(LAST_DAY(ADD_MONTHS(bucket_start, i))), 0
                    )
                )
            ) pts_query
        )

        WHEN p_unit IN ('month', 'year') THEN (
            SELECT to_json(named_struct(
                'years', transform(pts, p -> p.y),
                'months', transform(pts, p -> p.m),
                'values', transform(pts, p -> p.v),
                'n_days', transform(pts, p -> p.n)
            ))
            FROM (
                SELECT array_sort(collect_list(named_struct(
                    'y', bucket_year, 'm', bucket_month, 'v', bucket_value, 'n', n_days
                ))) AS pts
                FROM (
                    SELECT
                        bucket_year,
                        CASE WHEN p_unit = 'month' THEN MAX(bucket_month) END AS bucket_month,
                        -- ROUND needs a literal scale, so branch on the metric
                        IF(p_metric IN ('PRCP', 'SNOW', 'SNWD'),
                           ROUND(CASE WHEN p_period_aggregation = 'mean'  THEN AVG(value)
                                      WHEN p_period_aggregation = 'sum'   THEN SUM(value)
                                      WHEN p_period_aggregation = 'count' THEN SUM(hit)
                                      WHEN p_period_aggregation = 'any'   THEN MAX(hit) END, 2),
                           ROUND(CASE WHEN p_period_aggregation = 'mean'  THEN AVG(value)
                                      WHEN p_period_aggregation = 'sum'   THEN SUM(value)
                                      WHEN p_period_aggregation = 'count' THEN SUM(hit)
                                      WHEN p_period_aggregation = 'any'   THEN MAX(hit) END, 1)
                        ) AS bucket_value,
                        CAST(COUNT(value) AS INT) AS n_days,
                        -- first day of a year bucket's 12-month window (for coverage)
                        MAKE_DATE(
                            bucket_year - IF(COALESCE(p_year_start_month, 1) > 1, 1, 0),
                            COALESCE(p_year_start_month, 1), 1
                        ) AS bucket_start
                    FROM (
                        -- hit: 1 when the day meets the threshold (for count); computed
                        -- here because aggregates can't reference parameters
                        SELECT *, CASE
                                WHEN value IS NULL THEN NULL
                                WHEN (p_threshold_operator = '>'  AND value > p_threshold_value)
                                  OR (p_threshold_operator = '>=' AND value >= p_threshold_value)
                                  OR (p_threshold_operator = '<'  AND value < p_threshold_value)
                                  OR (p_threshold_operator = '<=' AND value <= p_threshold_value)
                                  OR (p_threshold_operator = '='  AND value = p_threshold_value)
                                THEN 1 ELSE 0
                            END AS hit
                        FROM (
                        SELECT
                            -- year buckets that start mid-year are labeled by the year they end in
                            CASE WHEN p_unit = 'year' AND COALESCE(p_year_start_month, 1) > 1
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
                          AND (p_month_filter IS NULL OR array_contains(p_month_filter, MONTH(obs_date)))
                        ) raw
                    ) days
                    GROUP BY
                        bucket_year,
                        CASE WHEN p_unit = 'month' THEN bucket_month END
                ) buckets
                -- coverage rule: >= 90% of the bucket's calendar days observed
                WHERE bucket_value IS NOT NULL
                  AND n_days >= 0.9 * CASE
                    WHEN p_unit = 'month' THEN DAY(LAST_DAY(MAKE_DATE(bucket_year, bucket_month, 1)))
                    -- days in the 12-month window, counting only p_month_filter months
                    ELSE AGGREGATE(
                        SEQUENCE(0, 11), 0,
                        (acc, i) -> acc + IF(
                            p_month_filter IS NULL OR array_contains(p_month_filter, MONTH(ADD_MONTHS(bucket_start, i))),
                            DAY(LAST_DAY(ADD_MONTHS(bucket_start, i))), 0
                        )
                    )
                  END
            ) pts_query
        )

        WHEN p_unit = 'month_of_year' THEN (
            SELECT to_json(named_struct(
                'months', transform(pts, p -> p.m),
                'values', transform(pts, p -> p.v),
                'stddev', transform(pts, p -> p.s),
                'n_years', transform(pts, p -> p.n)
            ))
            FROM (
                SELECT array_sort(collect_list(named_struct('m', m, 'v', v, 's', s, 'n', n))) AS pts
                FROM (
                    SELECT
                        bucket_month AS m,
                        IF(p_period_aggregation = 'any', ROUND(AVG(bucket_value), 3),
                           IF(p_metric IN ('PRCP', 'SNOW', 'SNWD'), ROUND(AVG(bucket_value), 2), ROUND(AVG(bucket_value), 1))) AS v,
                        IF(p_metric IN ('PRCP', 'SNOW', 'SNWD'), ROUND(STDDEV(bucket_value), 2), ROUND(STDDEV(bucket_value), 1)) AS s,
                        CAST(COUNT(*) AS INT) AS n
                    FROM (
                        SELECT
                            YEAR(obs_date) AS bucket_year,
                            MONTH(obs_date) AS bucket_month,
                            CASE WHEN p_period_aggregation = 'mean'  THEN AVG(value)
                                 WHEN p_period_aggregation = 'sum'   THEN SUM(value)
                                 WHEN p_period_aggregation = 'count' THEN SUM(hit)
                                 WHEN p_period_aggregation = 'any'   THEN MAX(hit)
                            END AS bucket_value,
                            COUNT(value) AS n_days
                        FROM (
                            SELECT *, CASE
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
                            ) raw
                        ) d
                        GROUP BY YEAR(obs_date), MONTH(obs_date)
                    ) monthly
                    -- coverage rule: >= 90% of the month's days observed
                    WHERE bucket_value IS NOT NULL
                      AND n_days >= 0.9 * DAY(LAST_DAY(MAKE_DATE(bucket_year, bucket_month, 1)))
                    GROUP BY bucket_month
                ) by_month
            ) pts_query
        )

        WHEN p_unit = 'week_of_year' THEN (
            SELECT to_json(named_struct(
                'weeks', transform(pts, p -> p.m),
                'values', transform(pts, p -> p.v),
                'stddev', transform(pts, p -> p.s),
                'n_years', transform(pts, p -> p.n)
            ))
            FROM (
                SELECT array_sort(collect_list(named_struct('m', m, 'v', v, 's', s, 'n', n))) AS pts
                FROM (
                    SELECT
                        bucket_month AS m,
                        IF(p_period_aggregation = 'any', ROUND(AVG(bucket_value), 3),
                           IF(p_metric IN ('PRCP', 'SNOW', 'SNWD'), ROUND(AVG(bucket_value), 2), ROUND(AVG(bucket_value), 1))) AS v,
                        IF(p_metric IN ('PRCP', 'SNOW', 'SNWD'), ROUND(STDDEV(bucket_value), 2), ROUND(STDDEV(bucket_value), 1)) AS s,
                        CAST(COUNT(*) AS INT) AS n
                    FROM (
                        SELECT
                            YEAR(obs_date) AS bucket_year,
                            LEAST(52, CAST(FLOOR((DAYOFYEAR(obs_date) - 1) / 7) AS INT) + 1) AS bucket_month,
                            CASE WHEN p_period_aggregation = 'mean'  THEN AVG(value)
                                 WHEN p_period_aggregation = 'sum'   THEN SUM(value)
                                 WHEN p_period_aggregation = 'count' THEN SUM(hit)
                                 WHEN p_period_aggregation = 'any'   THEN MAX(hit)
                            END AS bucket_value,
                            COUNT(value) AS n_days
                        FROM (
                            SELECT *, CASE
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
                            ) raw
                        ) d
                        GROUP BY YEAR(obs_date), LEAST(52, CAST(FLOOR((DAYOFYEAR(obs_date) - 1) / 7) AS INT) + 1)
                    ) monthly
                    -- coverage rule: 6 of a week's 7 days observed (week 52 runs to Dec 31)
                    WHERE bucket_value IS NOT NULL
                      AND n_days >= 6
                    GROUP BY bucket_month
                ) by_month
            ) pts_query
        )

        WHEN p_unit = 'season_of_year' THEN (
            SELECT to_json(named_struct(
                'seasons', transform(pts, p -> element_at(array('winter', 'spring', 'summer', 'fall'), p.k)),
                'values', transform(pts, p -> p.v),
                'stddev', transform(pts, p -> p.s),
                'n_years', transform(pts, p -> p.n)
            ))
            FROM (
                SELECT array_sort(collect_list(named_struct('k', k, 'v', v, 's', s, 'n', n))) AS pts
                FROM (
                    SELECT
                        season AS k,
                        IF(p_period_aggregation = 'any', ROUND(AVG(bucket_value), 3),
                           IF(p_metric IN ('PRCP', 'SNOW', 'SNWD'), ROUND(AVG(bucket_value), 2), ROUND(AVG(bucket_value), 1))) AS v,
                        IF(p_metric IN ('PRCP', 'SNOW', 'SNWD'), ROUND(STDDEV(bucket_value), 2), ROUND(STDDEV(bucket_value), 1)) AS s,
                        CAST(COUNT(*) AS INT) AS n
                    FROM (
                        SELECT
                            season_year,
                            season,
                            CASE WHEN p_period_aggregation = 'mean'  THEN AVG(value)
                                 WHEN p_period_aggregation = 'sum'   THEN SUM(value)
                                 WHEN p_period_aggregation = 'count' THEN SUM(hit)
                                 WHEN p_period_aggregation = 'any'   THEN MAX(hit)
                            END AS bucket_value,
                            COUNT(value) AS n_days
                        FROM (
                            SELECT *, CASE
                                WHEN value IS NULL THEN NULL
                                WHEN (p_threshold_operator = '>'  AND value > p_threshold_value)
                                  OR (p_threshold_operator = '>=' AND value >= p_threshold_value)
                                  OR (p_threshold_operator = '<'  AND value < p_threshold_value)
                                  OR (p_threshold_operator = '<=' AND value <= p_threshold_value)
                                  OR (p_threshold_operator = '='  AND value = p_threshold_value)
                                THEN 1 ELSE 0
                            END AS hit
                            FROM (
                            SELECT
                                -- December belongs to the following year's winter
                                YEAR(ADD_MONTHS(obs_date, 1)) AS season_year,
                                CASE
                                    WHEN MONTH(obs_date) IN (12, 1, 2) THEN 1
                                    WHEN MONTH(obs_date) IN (3, 4, 5) THEN 2
                                    WHEN MONTH(obs_date) IN (6, 7, 8) THEN 3
                                    ELSE 4
                                END AS season,
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
                              -- one month early so the first winter gets its December
                              AND obs_date BETWEEN ADD_MONTHS(p_start_date, -1) AND p_end_date
                            ) raw
                        ) d
                        WHERE season_year BETWEEN YEAR(p_start_date) AND YEAR(p_end_date)
                        GROUP BY season_year, season
                    ) seasonal
                    -- coverage rule: >= 90% of the season's days observed
                    WHERE bucket_value IS NOT NULL
                      AND n_days >= 0.9 * CASE season
                        WHEN 1 THEN DATEDIFF(MAKE_DATE(season_year, 3, 1), MAKE_DATE(season_year - 1, 12, 1))
                        ELSE DATEDIFF(
                            ADD_MONTHS(MAKE_DATE(season_year, 3 * season - 3, 1), 3),
                            MAKE_DATE(season_year, 3 * season - 3, 1)
                        )
                      END
                    GROUP BY season
                ) by_season
            ) pts_query
        )

        WHEN p_unit = 'day_of_year' THEN (
            SELECT to_json(named_struct(
                'days', transform(pts, p -> p.d),
                'values', transform(pts, p -> p.v),
                'stddev', transform(pts, p -> p.s),
                'n_years', transform(pts, p -> p.n),
                'max', transform(pts, p -> p.hi.v),
                'max_year', transform(pts, p -> p.hi.y),
                'min', transform(pts, p -> p.lo.v),
                'min_year', transform(pts, p -> -p.lo.ny)
            ))
            FROM (
                SELECT array_sort(collect_list(named_struct('d', d, 'v', v, 's', s, 'n', n, 'hi', hi, 'lo', lo))) AS pts
                FROM (
                    SELECT
                        d,
                        IF(p_period_aggregation = 'any', ROUND(AVG(hit), 3),
                           IF(p_metric IN ('PRCP', 'SNOW', 'SNWD'), ROUND(AVG(value), 2), ROUND(AVG(value), 1))) AS v,
                        IF(p_metric IN ('PRCP', 'SNOW', 'SNWD'), ROUND(STDDEV(value), 2), ROUND(STDDEV(value), 1)) AS s,
                        CAST(COUNT(value) AS INT) AS n,
                        -- struct max/min: the value, then the most recent year on a tie
                        MAX(named_struct('v', value, 'y', y)) AS hi,
                        MIN(named_struct('v', value, 'ny', -y)) AS lo
                    FROM (
                        SELECT *, CASE
                                WHEN value IS NULL THEN NULL
                                WHEN (p_threshold_operator = '>'  AND value > p_threshold_value)
                                  OR (p_threshold_operator = '>=' AND value >= p_threshold_value)
                                  OR (p_threshold_operator = '<'  AND value < p_threshold_value)
                                  OR (p_threshold_operator = '<=' AND value <= p_threshold_value)
                                  OR (p_threshold_operator = '='  AND value = p_threshold_value)
                                THEN 1 ELSE 0
                            END AS hit
                        FROM (
                        SELECT DATE_FORMAT(obs_date, 'MM-dd') AS d, YEAR(obs_date) AS y,
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
                        ) raw0
                    ) raw
                    WHERE value IS NOT NULL
                    GROUP BY d
                ) by_day
            ) pts_query
        )

        WHEN p_unit = 'value' THEN (
            SELECT to_json(named_struct(
                'values', transform(pts, p -> p.v),
                'counts', transform(pts, p -> p.c)
            ))
            FROM (
                SELECT array_sort(collect_list(named_struct('v', v, 'c', c))) AS pts
                FROM (
                    SELECT v, CAST(COUNT(*) AS INT) AS c
                    FROM (
                        -- rounded below the aggregate: aggregates can't reference parameters
                        SELECT IF(p_metric IN ('PRCP', 'SNOW', 'SNWD'), ROUND(value, 2), ROUND(value, 0)) AS v
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
                        ) raw
                        WHERE value IS NOT NULL
                    ) d
                    GROUP BY v
                ) by_value
            ) pts_query
        )

        WHEN p_unit = 'day' AND DATEDIFF(p_end_date, p_start_date) > 366 THEN
            to_json(named_struct('error', 'span_too_long', 'unit', p_unit))

        WHEN p_unit = 'day' THEN (
            SELECT to_json(named_struct(
                'dates', transform(pts, p -> p.d),
                'values', transform(pts, p -> p.v)
            ))
            FROM (
                SELECT array_sort(collect_list(named_struct('d', obs_date, 'v', v))) AS pts
                FROM (
                    -- rounded here: aggregates can't reference parameters
                    SELECT obs_date, IF(p_metric IN ('PRCP', 'SNOW', 'SNWD'), ROUND(value, 2), ROUND(value, 1)) AS v
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
                    ) raw
                    WHERE value IS NOT NULL
                ) d
            ) pts_query
        )

        ELSE to_json(named_struct('error', 'unsupported_unit', 'unit', p_unit))
    END
);
