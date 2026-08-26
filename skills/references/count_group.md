# Counting, events, and month/year aggregates

Read this file only when SKILL.md's Step 4 trigger rule says to — i.e. the
question refers to a precipitation/snow event/storm/spell, or treats a
calendar month or year itself as the thing being counted, compared, or
aggregated. It covers the extra fields `unit: "event"`, `unit: "month"`,
and `unit: "year"` require, on top of the `aggregation` table already
resolved in SKILL.md Step 4.

## `unit: "event"`

PRCP and SNOW only. An event is one or more *consecutive* days that each
meet a per-day threshold; a day below threshold ends the event, and the
next qualifying day (if any) starts a new one.

Requires:

- `event_day_threshold: {"operator": ..., "value": ...}` — the per-day
  cutoff defining event membership. Default: PRCP `>= 0.05`, SNOW `>= 0.5`
  (override only if the question states its own cutoff, e.g. "days with at
  least 1 inch of rain in a row").
- `event_value: "total" | "duration"` (default `"total"`) — which per-event
  number `aggregation` operates on:
  - `"total"` — sum of the metric across the event's member days. E.g. two
    days of 1.00" each, then a below-threshold day, then three days of
    0.45" each, is two events with totals 2.00" and 1.35" — not five
    qualifying days.
  - `"duration"` — the number of days in the event (its length), regardless
    of the metric's value. Use this for "how long", "how many days did it
    last", "the longest/shortest event" phrasing.

`aggregation` then applies over whichever per-event value `event_value`
selects, not the daily values:

- `count` → how many events occurred. Add `threshold` too only if the
  question also filters by size — "events over 2 inches" needs
  `event_value: "total"` + `threshold`; "events lasting more than 2 days"
  needs `event_value: "duration"` + `threshold`. Otherwise omit `threshold`
  and count every detected event.
- `max` / `min` → the biggest/smallest event by that value — "the biggest
  storm total" is `aggregation: "max"`, `event_value: "total"`; "the
  longest snow event" is `aggregation: "max"`, `event_value: "duration"`.
- `mean` / `median` → the average event value (average total, or average
  duration).
- `sum` with `event_value: "total"` → total across all events (equivalent
  to summing every qualifying day). `sum` with `event_value: "duration"` →
  total event-days; prefer the simpler `unit: "day"` count of qualifying
  days for that case instead, since it's the same number without the event
  grouping.

If asked about "events" for any metric other than PRCP/SNOW (e.g. "heat
wave events"), ask the user to define what qualifies rather than guessing.

## `unit: "month"` / `unit: "year"`

Buckets the date range into calendar months (each distinct year+month) or
calendar years (each distinct year), collapses each bucket's daily values
with `period_aggregation: "mean" | "sum"`, then applies `aggregation` to
those bucket values. Resolve `period_aggregation` the same way you'd
resolve a normal aggregation for that metric — "averaged below 32" →
`mean`; "exceeded 6 inches" → `sum`.

- `count` + `threshold` → how many months/years met a condition, e.g. "how
  many months exceeded 6 inches of rainfall" → `unit: "month"`,
  `period_aggregation: "sum"`, `threshold: {">", 6}`
- `max` / `min` → the single wettest/coldest/etc. month or year on record
- `mean` / `median` / `sum` → a summary across bucket values, e.g. "average
  monthly snowfall" → `unit: "month"`, `period_aggregation: "sum"`,
  `aggregation: "mean"`

`unit: "month"` also accepts an optional `month_filter`: an array of
calendar month numbers (1-12) restricting which months form buckets at all.
Use it when the question names a season or specific months instead of every
month in the range — "winter months" → `[12, 1, 2]` (same DJF/MAM/JJA/SON
definitions as SKILL.md Step 5). Each qualifying (year, month) instance
across the range is still counted separately — "winter months in the entire
record" means every individual December/January/February, not one pooled
average per month name. `unit: "year"` has no equivalent filter; if a
question implies a subset of years by some property other than the date
range itself (e.g. "El Niño years"), ask rather than guess.

## If it doesn't resolve cleanly

If the question names an event/month/year grain but the specifics
(threshold, `event_value`, period aggregation, month filter) can't be
confidently resolved, ask the user rather than guessing.

## Worked examples

**Q:** How many separate rainfall events did we have in the winter of 2025-26?
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "<resolved via lookup_station>", "metric": "PRCP", "aggregation": "count", "unit": "event", "start_date": "2025-12-01", "end_date": "2026-02-28", "event_day_threshold": {"operator": ">=", "value": 0.05}}
]}
```

**Q:** What was the total rainfall in the biggest rain event this winter?
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "<resolved via lookup_station>", "metric": "PRCP", "aggregation": "max", "unit": "event", "event_value": "total", "start_date": "2025-12-01", "end_date": "2026-02-28", "event_day_threshold": {"operator": ">=", "value": 0.05}}
]}
```

**Q:** How long did the longest snow event last this winter?
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "<resolved via lookup_station>", "metric": "SNOW", "aggregation": "max", "unit": "event", "event_value": "duration", "start_date": "2025-12-01", "end_date": "2026-02-28", "event_day_threshold": {"operator": ">=", "value": 0.5}}
]}
```

**Q:** How many rain events this winter lasted more than 2 days?
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "<resolved via lookup_station>", "metric": "PRCP", "aggregation": "count", "unit": "event", "event_value": "duration", "start_date": "2025-12-01", "end_date": "2026-02-28", "event_day_threshold": {"operator": ">=", "value": 0.05}, "threshold": {"operator": ">", "value": 2}}
]}
```

**Q:** How many winter months averaged below 32°F in the entire observation record at Dulles?
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00093738", "metric": "TAVG", "aggregation": "count", "unit": "month", "start_date": "1800-01-01", "end_date": "2026-08-04", "threshold": {"operator": "<", "value": 32}, "period_aggregation": "mean", "month_filter": [12, 1, 2]}
]}
```

**Q:** How many months exceeded 6" of rainfall between 2000 and 2020 at Dulles?
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00093738", "metric": "PRCP", "aggregation": "count", "unit": "month", "start_date": "2000-01-01", "end_date": "2020-12-31", "threshold": {"operator": ">", "value": 6}, "period_aggregation": "sum"}
]}
```
