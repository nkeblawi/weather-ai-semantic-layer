# Counting, events, and month/year aggregates

Read this file only when SKILL.md's Step 4 trigger rule says to — i.e. the question refers to a precipitation/snow event/storm/spell, treats a
calendar month or year itself as the thing being counted, compared, or aggregated, or groups values by season. It covers the extra fields
`unit: "event"`, `unit: "month"`, `unit: "year"`, `unit: "snow_season"`, and `unit: "season"` require, on top of the `aggregation` table already resolved in SKILL.md Step 4.

## `unit: "event"`

PRCP and SNOW only. An event is one or more *consecutive* days that each meet a per-day threshold; a day below threshold ends the event, and the
next qualifying day (if any) starts a new one.

Requires:

- `event_day_threshold: {"operator": ..., "value": ...}` — the per-day cutoff defining event membership. Default: PRCP `>= 0.05`, SNOW `>= 0.5`
  (override only if the question states its own cutoff, e.g. "days with at least 1 inch of rain in a row").
- `event_value: "total" | "duration"` (default `"total"`) — which per-event number `aggregation` operates on:
  - `"total"` — sum of the metric across the event's member days. E.g. two days of 1.00" each, then a below-threshold day, then three days of
    0.45" each, is two events with totals 2.00" and 1.35" — not five qualifying days.
  - `"duration"` — the number of days in the event (its length), regardless of the metric's value. Use this for "how long", "how many days did it
    last", "the longest/shortest event" phrasing.

`aggregation` then applies over whichever per-event value `event_value` selects, not the daily values:

- `count` → how many events occurred. Add `threshold` too only if the question also filters by size — "events over 2 inches" needs
  `event_value: "total"` + `threshold`; "events lasting more than 2 days" needs `event_value: "duration"` + `threshold`. Otherwise omit `threshold`
  and count every detected event.
- `max` / `min` → the biggest/smallest event by that value — "the biggest storm total" is `aggregation: "max"`, `event_value: "total"`; "the
  longest snow event" is `aggregation: "max"`, `event_value: "duration"`.
- `latest` / `earliest` → the most recent / first event *by date*, not by size — "the last snowstorm", "the most recent storm over 6 inches", "the
  first snow of the season". Returns that event's value plus its `event_start`/`event_end`. Add `threshold` when the question filters by
  size ("over 6 inches" → `event_value: "total"`, `threshold: {">", 6}`); the most recent event that passes the threshold is the one returned.
  Never use `max` for "last"/"most recent" — `max` is the *biggest* event, which is usually a different, older storm.
- `mean` / `median` → the average event value (average total, or average duration).
- `top` / `bottom` + `limit` → a ranked list of events by that value, for "the top 5 snowstorms" (`event_value: "total"`) or "the longest rain
  spells" (`event_value: "duration"`). See SKILL.md Step 4 for when to rank instead of using `max`/`min`. `threshold` doesn't apply.
- `sum` with `event_value: "total"` → total across all events (equivalent to summing every qualifying day). `sum` with `event_value: "duration"` →
  total event-days; prefer the simpler `unit: "day"` count of qualifying days for that case instead, since it's the same number without the event
  grouping.

If asked about "events" for any metric other than PRCP/SNOW (e.g. "heat wave events"), ask the user to define what qualifies rather than guessing.

## `unit: "month"` / `unit: "year"`

Buckets the date range into calendar months (each distinct year+month) or calendar years (each distinct year), collapses each bucket's daily values
with `period_aggregation: "mean" | "sum"`, then applies `aggregation` to those bucket values. Resolve `period_aggregation` the same way you'd
resolve a normal aggregation for that metric — "averaged below 32" → `mean`; "exceeded 6 inches" → `sum`.

- `count` + `threshold` → how many months/years met a condition, e.g. "how many months exceeded 6 inches of rainfall" → `unit: "month"`,
  `period_aggregation: "sum"`, `threshold: {">", 6}`
- `max` / `min` → the single wettest/coldest/etc. month or year on record
- `top` / `bottom` + `limit` → a ranked list of months or years ("the 5 snowiest years", "the coldest Januaries"). Months/years with under 90% of
  their days observed are left out of every answer, so the current partial year never shows up as "least snowy".
- `mean` / `median` / `sum` → a summary across bucket values, e.g. "average monthly snowfall" → `unit: "month"`, `period_aggregation: "sum"`,
  `aggregation: "mean"`

`unit: "month"` also accepts an optional `month_filter`: an array of calendar month numbers (1-12) restricting which months form buckets at all.
Use it when the question names a season or specific months instead of every month in the range — "winter months" → `[12, 1, 2]` (same DJF/MAM/JJA/SON
definitions as SKILL.md Step 5). Each qualifying (year, month) instance across the range is still counted separately — "winter months in the entire
record" means every individual December/January/February, not one pooled average per month name. `unit: "year"` has no equivalent filter (a `month_filter` on a year query is rejected, so never use one to stand in for a season); if a question implies a subset of years by some property other than the date range itself (e.g. "El Niño years"), ask rather than guess.

### Climate averages for a month name ("average January high")

(Single *days* within a month name — "the hottest January day", "ever hit 80 in January" — aren't climate averages: they're `unit: "day"`
with `month_filter`, as in SKILL.md Step 4.)

A month name with **no year**, asked as an average / typical / usual value ("What is the average high in January at Dulles?", "How much snow does
Chicago usually get in February?"), is a climate average over the **whole record** (never 1991-2020 unless the user says "normal" or names
years) — resolve it, don't ask:

- date range: `all_time` (SKILL.md Step 5)
- `unit: "month"`, `month_filter: [<that month>]`
- `period_aggregation`: `mean` for temperatures/wind, `sum` for PRCP/SNOW (a January's total), then `aggregation: "mean"` across those Januaries

**"Normal" is different:** "normal", "normals", "compared to / above / below normal" mean the **1991-2020** average (or the period the user
names), never the whole record. Same shape as above, but with `start_date` 1991-01-01 and `end_date` 2020-12-31 (`absolute_year_range`
1991-2020). "Was July 2024 warmer than normal?" is two queries: July 2024 itself, and the July normal.

A month name with no year that asks for one period's value instead ("How much snow fell in January?", "What was the high in March?") is the most
recent such month — use `recent_month` (SKILL.md Step 5), never a future month.

## `unit: "snow_season"`

SNOW only. A snow season runs July 1 - June 30 (the NWS convention), so one winter's snow stays together however early or late it falls — an October
storm and a May storm count toward the same season, at any latitude. Use it whenever snowfall is grouped by winter or season: "the snowiest winters",
"the least snowy seasons", "the snowiest winter on record". Never use `unit: "year"` for these: a calendar year splits one winter across two
years (December in one, January-February in the next).

It works like `unit: "year"`: `period_aggregation: "sum"` (a season's total), then `aggregation` across seasons — `max`/`min` for the single
snowiest/least snowy, `top`/`bottom` + `limit` for a ranking, `mean` for an average season, `count` + `threshold` for "how many winters had over 30
inches". Use the `all_time` date range unless the question limits the years; seasons are labeled by the year they end in, so "since 2000" means
`start_date` 1999-07-01 to keep the 1999-2000 season whole. Seasons with under 90% of days observed are left out of every answer, so the season under way never
counts.

## `unit: "season"`

Any metric grouped by meteorological season — one bucket per year's winter (Dec-Feb), spring (Mar-May), summer (Jun-Aug), or fall
(Sep-Nov): "the hottest summers", "the coldest winters", "the wettest springs", "an average summer high". Requires `season`: `winter`,
`spring`, `summer`, or `fall` ("autumn" is `fall`). Don't add `month_filter` — the app supplies the season's months. A winter is kept
whole across New Year (Dec 2009 - Feb 2010 is one winter).

It works like `unit: "year"`: `period_aggregation` collapses each season (`mean` for temperatures/wind, `sum` for PRCP), then
`aggregation` across seasons — `max`/`min`, `top`/`bottom` + `limit`, `mean`, or `count` + `threshold` ("how many summers averaged over
78°F"). Use the `all_time` date range unless the question limits the years. Snowfall by winter or season is `unit: "snow_season"` instead
(above), never `unit: "season"`.

One season's value in one year ("How hot was summer 2024?", "How much rain fell this past spring?") isn't a ranking or an aggregate
across seasons: that's a plain `unit: "day"` query over that season's date range (SKILL.md Step 5, `season`).

## If it doesn't resolve cleanly

If the question names an event/month/year grain but the specifics (threshold, `event_value`, period aggregation, month filter) can't be
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

**Q:** How much snow did Dulles Airport get in its last major snowstorm above 6 inches?

No time period stated, so search the whole record (SKILL.md Step 5). "Last" → `latest`; "how much snow" → `event_value: "total"`; "above 6 inches" →
`threshold`. Everything needed is stated, so resolve it — don't ask.
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00093738", "metric": "SNOW", "aggregation": "latest", "unit": "event", "event_value": "total", "start_date": "1800-01-01", "end_date": "2026-08-04", "event_day_threshold": {"operator": ">=", "value": 0.5}, "threshold": {"operator": ">", "value": 6}}
]}
```

**Q:** When was the first snow of this past winter, and how much fell?
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "<resolved via lookup_station>", "metric": "SNOW", "aggregation": "earliest", "unit": "event", "event_value": "total", "start_date": "2025-12-01", "end_date": "2026-02-28", "event_day_threshold": {"operator": ">=", "value": 0.5}}
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

**Q:** What is the average high and low temperature in January at Dulles?
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00093738", "metric": "TMAX", "aggregation": "mean", "unit": "month", "start_date": "1800-01-01", "end_date": "2026-08-04", "period_aggregation": "mean", "month_filter": [1]},
  {"station_id": "USW00093738", "metric": "TMIN", "aggregation": "mean", "unit": "month", "start_date": "1800-01-01", "end_date": "2026-08-04", "period_aggregation": "mean", "month_filter": [1]}
]}
```

**Q:** What's the normal July high at Dulles? ("normal" → 1991-2020, not the whole record)
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00093738", "metric": "TMAX", "aggregation": "mean", "unit": "month", "start_date": "1991-01-01", "end_date": "2020-12-31", "period_aggregation": "mean", "month_filter": [7]}
]}
```

**Q:** Was July 2024 warmer than normal at Reagan National? (the month itself, plus its 1991-2020 normal)
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00013743", "metric": "TAVG", "aggregation": "mean", "start_date": "2024-07-01", "end_date": "2024-07-31"},
  {"station_id": "USW00013743", "metric": "TAVG", "aggregation": "mean", "unit": "month", "start_date": "1991-01-01", "end_date": "2020-12-31", "period_aggregation": "mean", "month_filter": [7]}
]}
```

**Q:** What are the top 5 biggest snowstorms at Dulles?
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00093738", "metric": "SNOW", "aggregation": "top", "limit": 5, "unit": "event", "event_value": "total", "start_date": "1800-01-01", "end_date": "2026-08-04", "event_day_threshold": {"operator": ">=", "value": 0.5}}
]}
```

**Q:** Rank the snowiest winters at Dulles.
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00093738", "metric": "SNOW", "aggregation": "top", "limit": 3, "unit": "snow_season", "start_date": "1800-01-01", "end_date": "2026-08-04", "period_aggregation": "sum"}
]}
```

**Q:** How much snow does Dulles get in an average winter?
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00093738", "metric": "SNOW", "aggregation": "mean", "unit": "snow_season", "start_date": "1800-01-01", "end_date": "2026-08-04", "period_aggregation": "sum"}
]}
```

**Q:** What were the coldest winters at Dulles?
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00093738", "metric": "TAVG", "aggregation": "bottom", "limit": 3, "unit": "season", "season": "winter", "start_date": "1800-01-01", "end_date": "2026-08-04", "period_aggregation": "mean"}
]}
```

**Q:** Which were the 3 least snowy years at Dulles?
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00093738", "metric": "SNOW", "aggregation": "bottom", "limit": 3, "unit": "year", "start_date": "1800-01-01", "end_date": "2026-08-04", "period_aggregation": "sum"}
]}
```

**Q:** How many months exceeded 6" of rainfall between 2000 and 2020 at Dulles?
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00093738", "metric": "PRCP", "aggregation": "count", "unit": "month", "start_date": "2000-01-01", "end_date": "2020-12-31", "threshold": {"operator": ">", "value": 6}, "period_aggregation": "sum"}
]}
```
