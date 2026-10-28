# Unity Catalog Query Layer

How the SQL functions in `sql/uc/` rank, group, and filter results. For the
list of functions, see the [README](../README.md#6-unity-catalog-functions).

## Rankings (`top` / `bottom`)

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
  rank, then date) with `value` null; every other aggregation returns
  `rows: null`, so existing callers are unaffected.
  - day: `{rank, obs_date, value}`
  - event: `{rank, event_start, event_end, value, duration, total}`
  - month/year: `{rank, bucket_year, bucket_month, value, n_days}`

## Snow seasons

Snowfall grouped by winter ("the snowiest winters", "an average snow season")
uses the July 1 - June 30 snow season (the NWS convention), not the calendar
year, so one winter's snow isn't split across two years and October or May
snow still counts toward its season. `query_by_period`/`answer_query` take
`p_year_start_month` (default 1 = calendar year; the app passes 7 for snow
seasons), and a season is labeled by the year it ends in (2010 = "2009-10").

## Coverage rule (months, years, seasons)

A month, year, or season counts only if at least **90%** of its days have
data, in every answer and chart (averages, totals, counts, rankings, extremes).
This keeps partial periods, like the season under way or a station's first
year, from skewing results.
