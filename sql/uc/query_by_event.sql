CREATE OR REPLACE FUNCTION weather.analytics.query_by_event(
    p_station_id STRING,
    p_metric STRING,
    p_aggregation STRING,
    p_start_date DATE,
    p_end_date DATE,
    p_event_day_threshold_operator STRING,
    p_event_day_threshold_value DOUBLE,
    p_event_value STRING DEFAULT 'total',
    p_threshold_operator STRING DEFAULT NULL,
    p_threshold_value DOUBLE DEFAULT NULL,
    p_limit INT DEFAULT NULL
)
RETURNS STRING
COMMENT '
Answers a unit = event query from the weather query resolver skill against weather.analytics.observations. 

Returns JSON {value, event_start, event_end, rows, rows_truncated}. 
event_start/event_end are populated only when p_aggregation picks a single event -- max/min (the biggest/smallest by p_event_value) or latest/earliest (the most recent/first by date) -- and are null for mean/median/sum/stddev/count, which aggregate across many events with no single event to report.

p_metric is PRCP or SNOW only. 

p_event_day_threshold_operator/p_event_day_threshold_value define which days qualify to form an event (consecutive qualifying days = one event). 

p_event_value is "total" (sum of metric across an event) or "duration" (day count of an event); this is the per-event number p_aggregation operates over. 

p_threshold_operator/p_threshold_value are optional, only used to filter which events qualify when p_aggregation is count, latest, or earliest (e.g. "the last storm over 6 inches").

p_aggregation top/bottom ranks events by p_event_value (largest first / smallest first) with DENSE_RANK, so tied events share a rank. rows holds every event ranked 1..p_limit as {rank, event_start, event_end, value, duration, total}, ordered by rank then date; value/event_start/event_end are null. p_limit defaults to 3 and is clamped to 1-15; rows is capped at 15 entries, and rows_truncated is true when ties pushed more than 15 events into the ranks. rows is null for every other aggregation.
'
RETURN ((
    WITH qualifying_days AS (
        SELECT obs_date, value
        FROM (
            SELECT obs_date,
                CASE p_metric WHEN 'PRCP' THEN PRCP WHEN 'SNOW' THEN SNOW END AS value
            FROM weather.analytics.observations
            WHERE station_id = p_station_id
              AND obs_date BETWEEN p_start_date AND p_end_date
        ) d
        WHERE
            (p_event_day_threshold_operator = '>'  AND value > p_event_day_threshold_value) OR
            (p_event_day_threshold_operator = '>=' AND value >= p_event_day_threshold_value) OR
            (p_event_day_threshold_operator = '<'  AND value < p_event_day_threshold_value) OR
            (p_event_day_threshold_operator = '<=' AND value <= p_event_day_threshold_value) OR
            (p_event_day_threshold_operator = '='  AND value = p_event_day_threshold_value)
    ),
    grouped AS (
        SELECT obs_date, value,
            DATE_SUB(obs_date, CAST(ROW_NUMBER() OVER (ORDER BY obs_date) AS INT)) AS grp
        FROM qualifying_days
    ),
    events AS (
        SELECT
            MIN(obs_date) AS event_start,
            MAX(obs_date) AS event_end,
            SUM(value) AS event_total,
            COUNT(*) AS event_duration
        FROM grouped
        GROUP BY grp
    ),
    event_values AS (
        SELECT
            event_start, event_end,
            event_total, event_duration,
            CASE WHEN p_event_value = 'duration' THEN CAST(event_duration AS DOUBLE) ELSE event_total END AS ev_value
        FROM events
    ),
    sortable AS (
        SELECT *,
            -- SQL UDFs can't reference parameters in a window or an aggregate FILTER,
            -- so the ranking inputs are computed here as plain columns
            -- rounded so float noise (53.300000000000004 vs 53.3) doesn't split ties
            ROUND(CASE WHEN p_aggregation = 'bottom' THEN -ev_value ELSE ev_value END, 2) AS sort_key,
            p_aggregation IN ('top', 'bottom') AS is_ranking,
            GREATEST(LEAST(COALESCE(p_limit, 3), 15), 1) AS rank_limit
        FROM event_values
    ),
    ranked AS (
        SELECT *,
            -- top/bottom: 1 = largest/smallest; NULL values rank last and are dropped
            DENSE_RANK() OVER (ORDER BY sort_key DESC NULLS LAST) AS rk
        FROM sortable
    )
    SELECT to_json(named_struct(
        'value', COALESCE(
            CASE WHEN p_aggregation = 'mean'   THEN AVG(ev_value) END,
            CASE WHEN p_aggregation = 'median' THEN MEDIAN(ev_value) END,
            CASE WHEN p_aggregation = 'sum'    THEN SUM(ev_value) END,
            CASE WHEN p_aggregation = 'max'    THEN MAX(ev_value) END,
            CASE WHEN p_aggregation = 'min'    THEN MIN(ev_value) END,
            CASE WHEN p_aggregation = 'stddev' THEN STDDEV(ev_value) END,
            CASE WHEN p_aggregation = 'count'  THEN CAST(COUNT(*) AS DOUBLE) END,
            CASE WHEN p_aggregation = 'latest'   THEN MAX_BY(ev_value, event_start) END,
            CASE WHEN p_aggregation = 'earliest' THEN MIN_BY(ev_value, event_start) END
        ),
        'event_start',
            CASE
                WHEN p_aggregation = 'max'      THEN MAX_BY(event_start, ev_value)
                WHEN p_aggregation = 'min'      THEN MIN_BY(event_start, ev_value)
                WHEN p_aggregation = 'latest'   THEN MAX(event_start)
                WHEN p_aggregation = 'earliest' THEN MIN(event_start)
            END,
        'event_end',
            CASE
                WHEN p_aggregation = 'max'      THEN MAX_BY(event_end, ev_value)
                WHEN p_aggregation = 'min'      THEN MIN_BY(event_end, ev_value)
                WHEN p_aggregation = 'latest'   THEN MAX_BY(event_end, event_start)
                WHEN p_aggregation = 'earliest' THEN MIN_BY(event_end, event_start)
            END,
        'rows',
            CASE WHEN p_aggregation IN ('top', 'bottom') THEN
                slice(array_sort(
                    collect_list(named_struct(
                        'rank', CAST(rk AS INT), 'event_start', event_start,
                        'event_end', event_end, 'value', ev_value,
                        'duration', CAST(event_duration AS INT), 'total', event_total
                    )) FILTER (WHERE is_ranking AND ev_value IS NOT NULL AND rk <= rank_limit)
                ), 1, 15)
            END,
        'rows_truncated',
            CASE WHEN p_aggregation IN ('top', 'bottom') THEN
                COUNT(*) FILTER (WHERE is_ranking AND ev_value IS NOT NULL AND rk <= rank_limit) > 15
            END
    ))
    FROM ranked
    WHERE
        p_aggregation NOT IN ('count', 'latest', 'earliest') OR
        p_threshold_operator IS NULL OR
        (p_threshold_operator = '>'  AND ev_value > p_threshold_value) OR
        (p_threshold_operator = '>=' AND ev_value >= p_threshold_value) OR
        (p_threshold_operator = '<'  AND ev_value < p_threshold_value) OR
        (p_threshold_operator = '<=' AND ev_value <= p_threshold_value) OR
        (p_threshold_operator = '='  AND ev_value = p_threshold_value)
));
