# Unity Catalog Query Layer

How a resolved query is answered: what `wxcore` and the SQL functions in
`sql/uc/` each decide, how stations are found, and how results are ranked,
grouped, and filtered.

## What lives where

The semantic layer is two parts. Both front ends (the app and the MCP
server) go through `wxcore`, which calls `answer_query`.

**`wxcore`, before and after the SQL runs:**

- **Defaults** (`defaults.py`): fields a query leaves out, such as the
  metric and threshold for a named condition, the event-day cutoffs (0.05"
  of rain, 0.5" of snow), and the date range.
- **Checks** (`validation.py`): the allowed values and limits a query must
  fit, before anything reaches the warehouse.
- **Reshaping** (`db.call_answer_query`):
  - max/min ("hottest day in 1980") runs as a top/bottom ranking with a
    limit of 1;
  - first/last dates ("last spring freeze") and one calendar day's record
    or normal ("record high on July 4") run as a full series and are
    trimmed to the answer;
  - seasons run as year buckets that keep only the season's months, and
    snow seasons as years that start in July.
- **Station lookup fallbacks** (`db.call_lookup_station`): places and
  nearest stations, when the SQL `lookup_station` finds no station (see
  [Station lookup](#station-lookup)).

**SQL functions (`sql/uc/`):** station lookup by code, name, or alias, and
everything that reads observations: daily values, events, month/year
buckets, series, rankings and ties, and the 90% coverage rule below. A
ranking's default limit (3) is set here, not in `wxcore`; both cap it at 15,
and `wxcore` notes the trim in the answer ("top 20" gets 15 ranks).

## SQL functions

Deployed in Unity Catalog from `sql/uc/` with `scripts/deploy_sql.sh`; each
answers one resolved query at a time against the analytics tables.

- `answer_query`: the single entry point `wxcore` calls; dispatches to
  whichever function below applies
- `lookup_station`: resolves free text (city, airport code, alias) to a
  station
- `query_by_day` / `query_by_event` / `query_by_period`: answer a query at
  the daily, event (e.g. a rain event), or monthly/yearly grain
- `query_series`: returns every point of a chart instead of one value:
  daily (a year or less), monthly, yearly, seasonal, and snow-season series
  (means, sums, or counts of days past a threshold), climatologies by
  month, week, season, and calendar day (with each day's records), odds
  (the share of years a condition occurs), and histograms of daily values

## Station lookup

A station is found from its city, airport name or code, or a common alias
("Logan", "Sea-Tac", "Twin Cities") stored in `analytics.station_aliases`.
Case, spaces, and punctuation don't matter.

Any other town resolves by name through `analytics.places` (every place in
the world with 1,000+ people, from [GeoNames](https://www.geonames.org)).
Its coordinates give the nearest stations, and the user is asked which to
use ("There's no station in Vienna, VA. The nearest is Washington Dulles
International Airport (IAD), 10 miles away. Use it…?"), unless only one is
close by or the station's city is that place. A name several places share
("Springfield") asks which one is meant first.

## Normals, averages, and records

- A **normal** is computed from the station's own observations, 1991-2020
  by default (any 10-50 years allowed). Daily normals are smoothed over
  +/- 7 days.
- An **average** (with no years named) and a **record** cover the station's
  whole record.

## Events

Rain and snow only. An event is a run of consecutive days that each meet a
per-day cutoff: 0.05" of rain or 0.5" of snow unless the question names its
own. A day below the cutoff ends the event. Each event has a **total** (the
sum over its days) and a **duration** (its length in days); counts, means,
extremes, and rankings of events use one of the two. Two days of 1.00", a
dry day, then three days of 0.45" are two events, with totals of 2.00" and
1.35".

## Rankings

`answer_query` and the three `query_by_*` functions accept two ranking
aggregations, `top` (largest first) and `bottom` (smallest first), plus an
optional `p_limit`:

- **Ranks** Ranking uses `DENSE_RANK`, so tied values share a rank
  and the next distinct value takes the next rank (1, 2, 2, 3). `p_limit` is
  the number of *ranks* returned, so "top 3" can return more than 3 rows when
  there are ties.
- **Limits** `p_limit` defaults to 3 and is clamped to 1-15. The returned
  list is also capped at 15 rows; `rows_truncated: true` means ties pushed
  more rows into those ranks than were returned.
- **Tie detection** compares values rounded to 2 decimals, so floating-point
  noise (53.300000000000004 vs 53.3) doesn't split a tie.
- **Result shape** Ranking results come back in a `rows` array (ordered by
  rank, then date) with `value` null; see [Results](#results) for each
  row's fields.

## Snow seasons

Snowfall grouped by winter ("the snowiest winters", "an average snow season")
uses the July 1 - June 30 snow season (the NWS convention), not the calendar
year, so one winter's snow isn't split across two years and October or May
snow still counts toward its season. `query_by_period`/`answer_query` take
`p_year_start_month` (default 1 = calendar year; `wxcore` passes 7 for snow
seasons), and a season is labeled by the year it ends in (2010 = "2009-10").

## Coverage rule (months, years, seasons)

A month, year, or season counts only if at least **90%** of its days have
data, in every answer and chart (averages, totals, counts, rankings, extremes).
This keeps partial periods, like the season under way or a station's first
year, from skewing results.

## Results

Every function returns one JSON string; `answer_query` passes on the
result of the function it called. A field that doesn't apply to the
aggregation is null. Values are in °F and inches; `wxcore` converts them
for the user's units.

**`lookup_station`:** `{ghcn_id, name, city, state}`, or null when nothing
matches.

**Single values** (`query_by_day`, `query_by_event`, `query_by_period`):
`value` holds the answer, and `rows` and `rows_truncated` are set only for
`top`/`bottom`.

| Function | Fields | Set for max/min (or latest/earliest) | Each ranked row |
|---|---|---|---|
| `query_by_day` | `value`, `obs_date`, `rows`, `rows_truncated` | `obs_date` | `{rank, obs_date, value}` |
| `query_by_event` | `value`, `event_start`, `event_end`, `rows`, `rows_truncated` | `event_start`, `event_end` | `{rank, event_start, event_end, value, duration, total}` |
| `query_by_period` | `value`, `bucket_year`, `bucket_month`, `rows`, `rows_truncated`, `n_buckets`, `first_bucket`, `last_bucket` | `bucket_year`, `bucket_month` | `{rank, bucket_year, bucket_month, value, n_days}` |

`query_by_period` also says which periods the answer covers: `n_buckets`
complete months or years, from `first_bucket` to `last_bucket` (each
`{bucket_year, bucket_month}`; `bucket_month` is null for years).

**Series** (`query_series`, `aggregation: "series"`): parallel arrays, one
entry per point, chosen by `unit`:

| `unit` | Fields |
|---|---|
| `day` (366 days or less) | `dates`, `values` |
| `month`, `year` | `years`, `months`, `values`, `n_days` |
| `year` with `first_date`/`last_date` | `years`, `first`, `last`, `n_days` |
| `month_of_year` | `months`, `values`, `stddev`, `n_years` |
| `week_of_year` | `weeks`, `values`, `stddev`, `n_years` |
| `season_of_year` | `seasons`, `values`, `stddev`, `n_years` |
| `day_of_year` | `days` (`"MM-DD"`), `values`, `stddev`, `n_years`, `max`, `max_year`, `min`, `min_year` |
| `value` (histogram) | `values`, `counts` |

A `day` series over 366 days returns `{error: "span_too_long", unit}`, and
an unknown unit `{error: "unsupported_unit", unit}`.

**Reshaped by `wxcore`** (`db.call_answer_query`; see
[What lives where](#what-lives-where)):

- **max/min:** the rank-1 row of a top/bottom ranking. With a tie, it's the
  most recent one, and `ties` lists every rank-1 row. `n_buckets`,
  `first_bucket`, and `last_bucket` carry over for months and years.
- **first/last date:** for one year, `{value, bucket_year}`. For several,
  `typical_date` (the median), `earliest_on_record` and `latest_on_record`
  (a list when years tie), `data_years`, `years_counted`, and
  `years_without` (years with no such day).
- **One calendar day:** `{calendar_day, n_years, value}`, plus
  `record_year` for a record. Odds come back as a percent.
- No data gives `{value: null}`.

