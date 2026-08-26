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
    p_threshold_value DOUBLE DEFAULT NULL
)
RETURNS STRING
COMMENT '
Answers a unit = event query from the weather query resolver skill against weather.analytics.observations. 

Returns JSON {value, event_start, event_end}. 
event_start/event_end are populated only when p_aggregation is max or min (the date range of that specific winning event), and are null for mean/median/sum/stddev/count, which aggregate across many events with no single event to report. 

p_metric is PRCP or SNOW only. 

p_event_day_threshold_operator/p_event_day_threshold_value define which days qualify to form an event (consecutive qualifying days = one event). 

p_event_value is "total" (sum of metric across an event) or "duration" (day count of an event); this is the per-event number p_aggregation operates over. 

p_threshold_operator/p_threshold_value are optional, only used to filter which events count when p_aggregation is count.
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
            CASE WHEN p_event_value = 'duration' THEN CAST(event_duration AS DOUBLE) ELSE event_total END AS ev_value
        FROM events
    )
    SELECT to_json(named_struct(
        'value', COALESCE(
            CASE WHEN p_aggregation = 'mean'   THEN AVG(ev_value) END,
            CASE WHEN p_aggregation = 'median' THEN MEDIAN(ev_value) END,
            CASE WHEN p_aggregation = 'sum'    THEN SUM(ev_value) END,
            CASE WHEN p_aggregation = 'max'    THEN MAX(ev_value) END,
            CASE WHEN p_aggregation = 'min'    THEN MIN(ev_value) END,
            CASE WHEN p_aggregation = 'stddev' THEN STDDEV(ev_value) END,
            CASE WHEN p_aggregation = 'count'  THEN CAST(COUNT(*) AS DOUBLE) END
        ),
        'event_start',
            CASE
                WHEN p_aggregation = 'max' THEN MAX_BY(event_start, ev_value)
                WHEN p_aggregation = 'min' THEN MIN_BY(event_start, ev_value)
            END,
        'event_end',
            CASE
                WHEN p_aggregation = 'max' THEN MAX_BY(event_end, ev_value)
                WHEN p_aggregation = 'min' THEN MIN_BY(event_end, ev_value)
            END
    ))
    FROM event_values
    WHERE
        p_aggregation != 'count' OR
        p_threshold_operator IS NULL OR
        (p_threshold_operator = '>'  AND ev_value > p_threshold_value) OR
        (p_threshold_operator = '>=' AND ev_value >= p_threshold_value) OR
        (p_threshold_operator = '<'  AND ev_value < p_threshold_value) OR
        (p_threshold_operator = '<=' AND ev_value <= p_threshold_value) OR
        (p_threshold_operator = '='  AND ev_value = p_threshold_value)
));
