# Plotting: series, climatologies, normals, anomalies, trends

Read this file only when the **Plot mode** section below SKILL.md says this is a plot turn — either the user typed `/plot`, or this is a
follow-up to a plot. It extends SKILL.md's output with one more aggregation, `series`, which returns every month, year, or season as a
point on a chart instead of one value (or, over a year or less, every day). Everything else in SKILL.md still applies: Step 2 resolves the station, Step 3 the metric, Step 5
the date range (with the `resolve_date_range` tool), and Step 6 the JSON shape. `references/count_group.md` (loaded alongside this file)
defines the `season` and `snow_season` units used here.

## Step 1 — Is this a chart?

- **`/plot` turn** (the user typed `/plot`): the answer must be a chart. Emit `series` queries. If the request can't be charted — a single
  value ("/plot the hottest day ever"), or nothing that varies over days, months, years, or seasons — return `needs_clarification` asking
  what to chart (e.g. "A single day can't be charted. Would you like the number of 90°F days each year, or the average high for each
  month?").
- **Follow-up to a plot** (no `/plot`, but the previous turn was a chart): use `series` if the follow-up asks for another chart ("now do
  Reagan", "same for the 1961-1990 normals", "add a trend line"). If it asks for a single value instead ("which month was warmest?",
  "what was the normal July high?"), answer it as a regular SKILL.md query — no `series`.

## Step 2 — Choose the plot type and grain

Every `series` query needs `unit` and, except for daily points, `period_aggregation`: `mean` for temperatures/wind, `sum` for PRCP/SNOW
totals (as in `count_group.md`), or `count` for a number of days (below).

| The user wants | `unit` | Notes |
|---|---|---|
| Normals or records for **each day of the year** ("daily normal highs and lows", "the normal high for every day") | `day_of_year` | 366 points, smoothed; no `period_aggregation`; dates as for any climatology |
| Daily values over **a year or less** ("daily highs in 2024", "daily rainfall this summer", "snowfall day by day in winter 2009-10") | `day` | one point per day; no `period_aggregation` |
| Values over time, range up to 30 years | `month` | one point per month |
| Values over time, range over 30 years, or "annual"/"yearly" totals or averages | `year` | one point per year; no period stated → the whole record |
| One season across years ("summer highs since 1950", "winter lows") | `season` + `season` | one point per year's season; winter is Dec-Feb kept whole |
| Snowfall by winter / season | `snow_season` | July-June seasons, as in `count_group.md` |
| A trend ("is it getting warmer", "trend line") | `year`, or `season` for one season | never `month`: seasonal swings swamp a monthly trend |
| Climatology by month ("average high and low for each month", "monthly climatology") | `month_of_year` | 12 points |
| Climatology by season ("average temperature of each season") | `season_of_year` | 4 points (winter, spring, summer, fall) |

**Daily points only over a year or less** — a span of at most 366 days: a calendar year, a few months, or one snow season (Jul 1 to Jun
30). For a longer span, use `unit: "month"`; the app notes that daily points are limited to a year or less. A daily series takes no
`trend`, but it may take `baseline`/`display` ("daily highs in 2024 against normal"): it's compared with smoothed daily normals.

**Counting days per period** ("90-degree days per year", "freeze days each winter", "how many 70-degree nights each summer", "days
with snow each month"): `period_aggregation: "count"` plus `threshold`, with any unit except `day` — including the climatologies ("average
number of 90-degree days in each month"). Thresholds follow SKILL.md Step 4's counting idioms; also "90-degree days" → TMAX `>= 90`,
"100-degree days" → TMAX `>= 100`, "70-degree nights" / "warm nights" with a number → TMIN `>=` that number, "freeze days" → TMIN `< 32`.
A count series works with `baseline`, `display`, and `trend` like any other (e.g. "90-degree days per year with a trend line").

A climatology covers the date range you give it: no period stated → the whole record (`all_time`); "the 1991-2020 normals" → that range
(next step).

## Step 3 — Normals (30-year climatologies)

"Normal(s)", "normal range", "compared to normal", "anomalies", "departure from normal" refer to a **baseline period**:

- No period stated → **1991-2020** (the current standard).
- A standard 30-year period named ("1961-1990 normals", "1971-2000", "1981-2010", "1991-2020") → that period.
- Any other span of **10 to 50 years** the user asks for ("normals for 2001-2020") → that span.
- Under 10 or over 50 years → `needs_clarification` asking for a period of 10 to 50 years.

How the baseline is expressed depends on what's plotted:

- **The normals themselves** ("plot the 1991-2020 monthly normals", "show the normal high for each month"): a climatology
  (`month_of_year` or `season_of_year`) whose `start_date`/`end_date` span the baseline years (Jan 1 of the first year to Dec 31 of the
  last). No `baseline` field.
- **A period plotted against the normals** ("plot 2025 against normal", "how did this summer compare to normal"): a time series (`month`,
  `year`, `season`, or `snow_season`) over the period being compared, plus `baseline: {"start_year": ..., "end_year": ...}` and
  `display`:
  - `"overlay"` (default) — the values next to the normal line and its normal range.
  - `"anomaly"` — each value minus its normal, when the user says anomaly / departure / above or below normal over time.
  - `"climate"` — a daily climate graph (NWS style): daily highs/lows over the normal range and the record range. Only `unit: "day"`
    (a year or less) with TMAX and/or TMIN (one query each); needs `baseline`. Use it for "climate graph", "daily highs and lows vs normal
    and records", "how is this year/month going compared to normal and records".
  - `"cumulative"` — a running total of daily PRCP or SNOW ("year-to-date rain", "accumulated snowfall this season", "running total").
    Only `unit: "day"`; `baseline` is optional and adds the normal accumulation. Several seasons on one chart ("this winter vs 2009-10
    vs 2015-16") → one query per season (each Jul 1 - Jun 30 for snow), same station; the app lines them up by day of season.

The app computes normals and anomalies itself from `baseline`; never add a second query for the normals.

## Step 4 — Trend lines

Add `"trend": true` only when the question asks for a trend, trend line, warming, cooling, or "is it getting …". Never on a
climatology (`month_of_year`, `season_of_year`). The app fits the line and reports the slope per decade.

## Step 5 — Several lines on one chart

One query per line, as in SKILL.md: "high and low" → a TMAX and a TMIN query (same unit and dates); "Dulles vs Reagan" → one query per
station. At most 4 queries. They're drawn on one chart when they share a unit of measure.

## Schema additions

On top of SKILL.md Step 6's query fields:

```json
{
  "aggregation": "series",
  "unit": "day | month | year | season | snow_season | month_of_year | season_of_year | day_of_year",
  "period_aggregation": "mean | sum | count (required, except omitted for unit day)",
  "threshold": {"operator": "> | >= | < | <= | =", "value": "number, only with period_aggregation count"},
  "season": "winter | spring | summer | fall, only with unit season",
  "trend": "true, only when a trend is asked for; omit otherwise",
  "baseline": {"start_year": "first year of the normals period", "end_year": "last year"},
  "display": "overlay | anomaly | climate | cumulative (overlay/anomaly/climate need baseline; climate/cumulative need unit day)"
}
```

## Worked examples

**Q:** /plot the monthly climatology of high and low temperatures at Dulles
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00093738", "metric": "TMAX", "aggregation": "series", "unit": "month_of_year", "period_aggregation": "mean", "start_date": "1800-01-01", "end_date": "2026-08-04"},
  {"station_id": "USW00093738", "metric": "TMIN", "aggregation": "series", "unit": "month_of_year", "period_aggregation": "mean", "start_date": "1800-01-01", "end_date": "2026-08-04"}
]}
```

**Q:** /plot the 1991-2020 normal high for each month at LaGuardia
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00014732", "metric": "TMAX", "aggregation": "series", "unit": "month_of_year", "period_aggregation": "mean", "start_date": "1991-01-01", "end_date": "2020-12-31"}
]}
```

**Q:** /plot monthly temperatures at Dulles in 2025 against normal
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00093738", "metric": "TAVG", "aggregation": "series", "unit": "month", "period_aggregation": "mean", "start_date": "2025-01-01", "end_date": "2025-12-31", "baseline": {"start_year": 1991, "end_year": 2020}, "display": "overlay"}
]}
```

**Q:** /plot yearly temperature anomalies at Reagan National since 1950 relative to 1961-1990
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00013743", "metric": "TAVG", "aggregation": "series", "unit": "year", "period_aggregation": "mean", "start_date": "1950-01-01", "end_date": "2026-08-04", "baseline": {"start_year": 1961, "end_year": 1990}, "display": "anomaly"}
]}
```

**Q:** /plot summer highs at Dulles with a trend line
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00093738", "metric": "TMAX", "aggregation": "series", "unit": "season", "season": "summer", "period_aggregation": "mean", "start_date": "1800-01-01", "end_date": "2026-08-04", "trend": true}
]}
```

**Q:** /plot annual snowfall at LaGuardia (snow by winter → snow seasons; no period → whole record)
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00014732", "metric": "SNOW", "aggregation": "series", "unit": "snow_season", "period_aggregation": "sum", "start_date": "1800-01-01", "end_date": "2026-08-04"}
]}
```

**Q:** /plot annual precipitation at Dulles
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00093738", "metric": "PRCP", "aggregation": "series", "unit": "year", "period_aggregation": "sum", "start_date": "1800-01-01", "end_date": "2026-08-04"}
]}
```

**Q:** /plot is Dulles getting warmer? (a trend → yearly points + trend line; no period → whole record)
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00093738", "metric": "TAVG", "aggregation": "series", "unit": "year", "period_aggregation": "mean", "start_date": "1800-01-01", "end_date": "2026-08-04", "trend": true}
]}
```

**Q:** /plot monthly rainfall at Dulles in 2024
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00093738", "metric": "PRCP", "aggregation": "series", "unit": "month", "period_aggregation": "sum", "start_date": "2024-01-01", "end_date": "2024-12-31"}
]}
```

**Q:** /plot daily high and low temperatures at Dulles in 2023 (one year → daily points)
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00093738", "metric": "TMAX", "aggregation": "series", "unit": "day", "start_date": "2023-01-01", "end_date": "2023-12-31"},
  {"station_id": "USW00093738", "metric": "TMIN", "aggregation": "series", "unit": "day", "start_date": "2023-01-01", "end_date": "2023-12-31"}
]}
```

**Q:** /plot the normal high and low for each day of the year at Dulles ("normal" → 1991-2020)
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00093738", "metric": "TMAX", "aggregation": "series", "unit": "day_of_year", "start_date": "1991-01-01", "end_date": "2020-12-31"},
  {"station_id": "USW00093738", "metric": "TMIN", "aggregation": "series", "unit": "day_of_year", "start_date": "1991-01-01", "end_date": "2020-12-31"}
]}
```

**Q:** /plot daily highs at Dulles in 2024 against normal
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00093738", "metric": "TMAX", "aggregation": "series", "unit": "day", "start_date": "2024-01-01", "end_date": "2024-12-31", "baseline": {"start_year": 1991, "end_year": 2020}, "display": "overlay"}
]}
```

**Q:** /plot a climate graph for Dulles in 2024 (daily highs and lows vs normals and records)
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00093738", "metric": "TMAX", "aggregation": "series", "unit": "day", "start_date": "2024-01-01", "end_date": "2024-12-31", "baseline": {"start_year": 1991, "end_year": 2020}, "display": "climate"},
  {"station_id": "USW00093738", "metric": "TMIN", "aggregation": "series", "unit": "day", "start_date": "2024-01-01", "end_date": "2024-12-31", "baseline": {"start_year": 1991, "end_year": 2020}, "display": "climate"}
]}
```

**Q:** /plot year-to-date rainfall at Reagan National compared to normal
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00013743", "metric": "PRCP", "aggregation": "series", "unit": "day", "start_date": "2026-01-01", "end_date": "2026-08-04", "baseline": {"start_year": 1991, "end_year": 2020}, "display": "cumulative"}
]}
```

**Q:** /plot accumulated snowfall at Dulles in 2009-10 vs 2015-16
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00093738", "metric": "SNOW", "aggregation": "series", "unit": "day", "start_date": "2009-07-01", "end_date": "2010-06-30", "display": "cumulative"},
  {"station_id": "USW00093738", "metric": "SNOW", "aggregation": "series", "unit": "day", "start_date": "2015-07-01", "end_date": "2016-06-30", "display": "cumulative"}
]}
```

**Q:** /plot daily snowfall at Dulles in winter 2009-10 (one snow season → daily points)
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00093738", "metric": "SNOW", "aggregation": "series", "unit": "day", "start_date": "2009-07-01", "end_date": "2010-06-30"}
]}
```

**Q:** /plot daily highs at Dulles from 2020 to 2023 (over a year → monthly points)
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00093738", "metric": "TMAX", "aggregation": "series", "unit": "month", "period_aggregation": "mean", "start_date": "2020-01-01", "end_date": "2023-12-31"}
]}
```

**Q:** /plot the number of 90-degree days per year at Dulles with a trend line
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00093738", "metric": "TMAX", "aggregation": "series", "unit": "year", "period_aggregation": "count", "threshold": {"operator": ">=", "value": 90}, "start_date": "1800-01-01", "end_date": "2026-08-04", "trend": true}
]}
```

**Q:** /plot freeze days each winter at Reagan National since 1970 (a whole cold season → snow-season years, Jul-Jun)
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00013743", "metric": "TMIN", "aggregation": "series", "unit": "snow_season", "period_aggregation": "count", "threshold": {"operator": "<", "value": 32}, "start_date": "1969-07-01", "end_date": "2026-08-04"}
]}
```

**Q:** /plot the average number of 90-degree days in each month at Dulles
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00093738", "metric": "TMAX", "aggregation": "series", "unit": "month_of_year", "period_aggregation": "count", "threshold": {"operator": ">=", "value": 90}, "start_date": "1800-01-01", "end_date": "2026-08-04"}
]}
```

**Q:** /plot the hottest day ever at Dulles
```json
{"current_date": "2026-08-04", "status": "needs_clarification", "clarification_question": "A single day can't be charted. Would you like the number of 90°F days each year, or the average high for each month?"}
```

**Follow-up**, after the monthly climatology chart above — **Q:** now do Reagan National
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00013743", "metric": "TMAX", "aggregation": "series", "unit": "month_of_year", "period_aggregation": "mean", "start_date": "1800-01-01", "end_date": "2026-08-04"},
  {"station_id": "USW00013743", "metric": "TMIN", "aggregation": "series", "unit": "month_of_year", "period_aggregation": "mean", "start_date": "1800-01-01", "end_date": "2026-08-04"}
]}
```

**Follow-up**, after the same chart — **Q:** what's the normal July high there? (a single value: a regular query, no `series`; "normal" → 1991-2020)
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00093738", "metric": "TMAX", "aggregation": "mean", "unit": "month", "period_aggregation": "mean", "month_filter": [7], "start_date": "1991-01-01", "end_date": "2020-12-31"}
]}
```
