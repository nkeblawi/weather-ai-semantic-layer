CREATE OR REPLACE FUNCTION weather.analytics.answer_query(
    p_station_id STRING,
    p_metric STRING,
    p_aggregation STRING,
    p_start_date DATE,
    p_end_date DATE,
    p_unit STRING,
    p_threshold_operator STRING DEFAULT NULL,
    p_threshold_value DOUBLE DEFAULT NULL,
    p_event_day_threshold_operator STRING DEFAULT NULL,
    p_event_day_threshold_value DOUBLE DEFAULT NULL,
    p_event_value STRING DEFAULT 'total',
    p_period_aggregation STRING DEFAULT NULL,
    p_month_filter ARRAY<INT> DEFAULT NULL
)
RETURNS STRING
COMMENT '
Single entry point for answering one resolved query entry from the weather query resolver skill. 

Dispatches internally based on p_unit ("day", "event", "month", or "year") to query_by_day, query_by_event, or query_by_period, and returns that function''s JSON result directly. 

Pass every field from the resolved query entry that applies; leave the rest null. 

p_event_day_threshold_operator/p_event_day_threshold_value are required when p_unit is event. 
p_period_aggregation is required when p_unit is month or year. 
p_threshold_operator/p_threshold_value are required when p_aggregation is count and p_unit is day or month/year, optional when p_unit is event.
'
RETURN (
    CASE
        WHEN p_unit = 'day' THEN
            weather.analytics.query_by_day(
                p_station_id, p_metric, p_aggregation, p_start_date, p_end_date,
                p_threshold_operator, p_threshold_value
            )
        WHEN p_unit = 'event' THEN
            weather.analytics.query_by_event(
                p_station_id, p_metric, p_aggregation, p_start_date, p_end_date,
                p_event_day_threshold_operator, p_event_day_threshold_value,
                p_event_value, p_threshold_operator, p_threshold_value
            )
        WHEN p_unit IN ('month', 'year') THEN
            weather.analytics.query_by_period(
                p_station_id, p_metric, p_aggregation, p_start_date, p_end_date,
                p_unit, p_period_aggregation, p_threshold_operator, p_threshold_value,
                p_month_filter
            )
    END
);
