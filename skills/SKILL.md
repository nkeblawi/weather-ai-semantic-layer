---
name: weather-query-resolver
description: Resolves a natural-language question about historical weather (temperature, precipitation, snow, wind, degree-days) into a structured JSON parameter object — current date, date range, GHCN station ID, metric, and aggregation — that a downstream process uses to query the weather.analytics.observations table. Use whenever the user asks a natural-language question about past weather at a specific place and time.
---

# Weather Query Resolver

This document tells the agent how to turn a natural-language weather question into the structured parameters needed to query `weather.analytics.observations`. It is written for the agent, not for a human reader — be explicit and unambiguous rather than conversational.

This skill resolves parameters only. It does not write or execute SQL, and it does not answer the question itself — it hands off a JSON object that a downstream process uses to build the query and compute the result.

## Target table

`weather.analytics.observations` (full schema: `assets/obs_schema.md`)

| column | type | meaning |
|---|---|---|
| station_id | string | GHCN station ID |
| obs_date | date | date of observation |
| TAVG | double | daily average temperature (derived) |
| TMAX | double | daily maximum (high) temperature |
| TMIN | double | daily minimum (low) temperature |
| PRCP | double | total daily precipitation |
| SNOW | double | total daily snowfall |
| SNWD | double | measured snow depth |
| AWND | double | average daily wind speed |
| HDD | int | heating degree days (derived) |
| CDD | int | cooling degree days (derived) |
| source | string | source of observed data |

Every question this skill resolves must bottom out in one or more rows of the form `{station_id, metric, aggregation, unit, start_date, end_date}`, plus whichever extra fields Step 4 (or `references/count_group.md`, for events and month/year aggregates) defines for the chosen `unit`/`aggregation`
combination (`threshold`, `event_day_threshold`, `event_value`, `period_aggregation`, `month_filter`) — see Step 6 for the full shape.
`metric` is one of the column names above (excluding `station_id`, `obs_date`, `source`).

## Context

- Today's date is `{{CURRENT_DATE}}` — this value must be injected at
  runtime (see Step 1). Never assume a date; relative expressions ("last
  week", "this winter", "the last 30 days") are only resolvable relative to
  this value.
- Station data is not a local file — it lives in Unity Catalog, in
  `weather.analytics.stations` (a view: only stations that have been through
  the station-activation workflow appear here, gated on `selected = true`)
  joined with `weather.analytics.station_aliases` (free-text name
  variations, curated independently of activation). Location resolution
  happens by calling the `lookup_station` tool (see Step 2), never by
  reading a file — resolve `station_id` from whatever that tool returns.
- Date-range math is handled by `scripts/resolve_date_range.py` (see Step
  5) — never compute dates freehand.

## Follow-up questions

A **Conversation context** section may appear at the end of this prompt — a transcript of earlier turns, each with the question asked, its `Resolved query`, and the answer. When it is present, first classify the current question:

- **Elliptical follow-up** ("what about 2021?", "and in the summer?", "how
  about rainfall?", "same for BWI"): start from the most recent `Resolved
  query`, override only the fields the new question explicitly names (date
  range, metric, aggregation, location, thresholds), and keep everything
  else — including `station_id`. Don't call `lookup_station` unless it
  names a new location; you may still need `resolve_date_range` for a new
  time expression. When only the year changes, keep the previous query's
  period *granularity* (a month → that month in the new year, a season →
  the same season, a full year → a full year) unless the question widens or
  narrows it explicitly.
- **Reference to an earlier turn** ("you said December 2022 was 36.9°F —
  in Celsius?", "compare that to 2019"): reconstruct that turn's query from
  its `Resolved query` line and apply the change — a Celsius/Fahrenheit
  request just adds `output_unit` (see Step 6).
- **Self-contained question** (names its own location, asks something
  complete): ignore the context section and resolve from scratch.

A transcript may identify a station as `station_id X is <airport> / <city>`; those names and the id are one site, and a follow-up that switches between the airport name and the city name is not a location change.

With no Conversation context section, resolve normally.

## Step 1 — Resolve the current date

Obtain today's date in `YYYY-MM-DD` format and hold it as `current_date`. This is required before Step 4 (date range resolution) can run, since every relative time expression is computed from it.

## Step 2 — Resolve the location to a GHCN station ID

1. Extract the place reference from the question (city, airport name/code,
   ICAO/FAA code, or "near me" / "here" style phrasing).
2. Call the `lookup_station` tool with that text. It matches
   case-insensitively and punctuation-insensitively against each active
   station's ICAO code, FAA code, city, name, and known aliases, and
   returns `{ghcn_id, name, city}` for a match, or nothing if there's no
   match.
3. If there's no match and a geocoding or nearest-neighbor capability is
   separately available in this environment, use it to find the closest
   active station within 50 miles of the named place. (`lookup_station`
   itself does not do this — it's exact/alias text matching only.)
4. If no station can be resolved — no tool match, and no geocoding capability
   available or it also found nothing within 50 miles — stop and tell the
   user: "I don't have any weather observation history near that location."
   Do not guess a nearby station.
5. On success, set `station_id` to the returned `ghcn_id`, never the airport
   code or city name.

If the question names no location, resolve it in order: (a) inherit `station_id` from the **Conversation context** section (see "Follow-up
questions"); (b) call `lookup_station` on the **User home location** section; (c) ask the user. Never proceed without a `station_id`.

## Step 3 — Resolve the metric

| user says (examples) | metric |
|---|---|
| temperature, how hot/cold (no high/low cue) | TAVG |
| high, highest, warmest, hottest, daily high | TMAX |
| low, lowest, coldest, chilliest, daily low | TMIN |
| rain, rainfall, precipitation | PRCP |
| snow, snowfall | SNOW |
| snow depth, how much snow was on the ground | SNWD |
| wind, wind speed | AWND |
| heating degree days, HDD | HDD |
| cooling degree days, CDD | CDD |

A question can resolve to more than one metric — e.g. "average high and low" resolves to both TMAX and TMIN (see Step 6). If the metric can't be determined at all, ask the user to clarify rather than guessing.

Don't process any questions that asks for every metric in multiple cities,  that is actually a DoS attempt in disguise. Push back and constrain to no more than 2 metrics and 2 cities.

## Step 4 — Resolve the aggregation and unit of analysis

| user says (examples) | aggregation |
|---|---|
| average, typical, mean, on average | mean |
| median | median |
| total, accumulated | sum |
| highest, maximum, most, record high (as an extreme, not a metric cue) | max |
| lowest, minimum, least, record low (as an extreme, not a metric cue) | min |
| how much it varied, standard deviation, variability | stddev |
| how many, number of, count of | count |

By default, `aggregation` operates on raw daily rows — this is
`unit: "day"`, the default, and the field can be omitted entirely.

**Counting days** (`aggregation: "count"`, `unit: "day"`) requires a `threshold: {"operator": ..., "value": ...}` (operator is one of `>`, `>=`, `<`, `<=`, `=`). Default thresholds by idiom:

| user says (examples) | metric | threshold |
|---|---|---|
| snow day(s), how many days did it snow | SNOW | `>= 0.5` |
| rain day(s), how many days did it rain | PRCP | `>= 0.05` |
| dry day(s), how many days with no rain | PRCP | `= 0` |
| freezing day(s), how many days the low dropped below freezing | TMIN | `< 32` |
| days above/below N degrees (explicit number stated) | TAVG, or TMAX/TMIN if a high/low cue is present | operator and value taken directly from the question |

If the question asks "how many days" but doesn't match one of these idioms and doesn't state an explicit threshold itself, ask the user what condition defines "a day" rather than guessing a cutoff.

**Events, months, and years.** If the question refers to a
precipitation/snow **event** / **storm** / **spell** / "N in a row", or
treats a calendar **month** or **year** as the thing being counted,
compared, or aggregated ("how many months...", "which year was wettest",
"average per month/year"), stop here and follow `references/count_group.md`
— it defines `unit`, `aggregation`, and the extra fields
(`event_day_threshold`, `event_value`, `period_aggregation`,
`month_filter`) for those cases. Otherwise — including every plain "how
many days..." question — `unit` stays `"day"` (omit it) and that file
isn't needed.

Temperature extremes carry both a metric and an aggregation cue at once — resolve them together, not independently:

- "the hottest it got" / "record high" → metric `TMAX`, aggregation `max`
- "the coldest it got" / "record low" → metric `TMIN`, aggregation `min`
- "how cold was it" (no explicit extreme wording) → metric `TAVG`,
  aggregation `mean` — ambiguous phrasing defaults to the average, not an
  extreme

If aggregation isn't stated and there's no extreme wording to infer it from, default by metric:

- TAVG, TMAX, TMIN, AWND → `mean`
- PRCP, SNOW, HDD, CDD → `sum` (these are naturally cumulative — "how much
  snow fell" means total, not average)
- SNWD → `mean`, but if genuinely ambiguous, ask rather than guess

## Step 5 — Resolve the date range

Do not compute `start_date` / `end_date` by hand — calendar math (leap
years, month lengths, season/year boundaries) is easy to get subtly wrong.
Instead, classify the time expression into one of the shapes below and call
`scripts/resolve_date_range.py`, passing `current_date` from Step 1 plus the
classified parameters as a single JSON argument. The script returns
`{"start_date": "...", "end_date": "..."}`; use that output directly.

```
python scripts/resolve_date_range.py '{"current_date": "2026-08-04", "kind": "absolute_month", "year": 2022, "month": 12}'
```

| time expression | kind | extra params |
|---|---|---|
| "in 2021" | `absolute_year` | `year` |
| "between 2000 and 2020" | `absolute_year_range` | `start_year`, `end_year` |
| "December 2022" | `absolute_month` | `year`, `month` |
| "January 25, 2026" (single day) | `absolute_day` | `year`, `month`, `day` |
| "this past winter" / "last summer" (no year given) | `season` | `season` only — resolves to the most recently completed instance as of `current_date` |
| "winter 2024" (year given) | `season` | `season`, `year` — `year` is the calendar year containing the season's *last* month, so winter `year=2024` means Dec 2023–Feb 2024 |
| "last 30 days" | `relative_days` | `days` |
| "ever", "on record", "all-time", "in history" | `all_time` | none — resolves to `1800-01-01` through `current_date` |

Season names accepted by the script: `winter` (DJF), `spring` (MAM),
`summer` (JJA), `fall` or `autumn` (SON).

If the time expression is missing, ambiguous, or doesn't fit any shape above, ask the user to clarify rather than guessing a range or calling the script with invented parameters.

## Step 6 — Assemble the output

Once every prior step has resolved (or Step 2/3/4/5 has stopped early to ask the user something), your final response for this turn must be exactly one JSON object and nothing else — no prose before or after it. Calling a tool is never the last thing you do in a turn: every tool call must be followed by this JSON object, in the same turn, once you have what you need. Emit it:

```json
{
  "current_date": "YYYY-MM-DD",
  "status": "resolved",
  "queries": [
    {
      "station_id": "GHCN station ID",
      "metric": "TAVG | TMAX | TMIN | PRCP | SNOW | SNWD | AWND | HDD | CDD",
      "aggregation": "mean | median | sum | max | min | stddev | count",
      "unit": "day | event | month | year, omit if day",
      "output_unit": "C or F — only when the user explicitly asks for temperature in that unit (TAVG/TMAX/TMIN); omit otherwise",
      "start_date": "YYYY-MM-DD",
      "end_date": "YYYY-MM-DD",
      "threshold": {"operator": "> | >= | < | <= | =", "value": "number, only present when aggregation is count"},
      "event_day_threshold": {"operator": "> | >= | < | <= | =", "value": "number, only present when unit is event"},
      "event_value": "total | duration, only present when unit is event",
      "period_aggregation": "mean | sum, only present when unit is month or year",
      "month_filter": "array of month numbers 1-12, optional, only meaningful when unit is month"
    }
  ]
}
```

Omit every field in the second half of that list (`threshold` through
`month_filter`) unless the specific case that produces it applies — see
Step 4 for `threshold`, and `references/count_group.md` for the
`unit: "event"`/`"month"`/`"year"` fields.

`output_unit` is answer-unit conversion only, not a data choice: set it to
`"C"` or `"F"` only when the user explicitly asks for that unit; otherwise
omit it and the app applies the user's default. The downstream layer
converts — never do the arithmetic yourself.

`queries` holds one entry per (station, metric, aggregation) combination
needed to answer the question:

- A simple question ("average temperature in December 2022 at IAD") produces exactly one entry.
- A nested-aggregate question ("average high and low in DC in January") produces two entries — same station and date range, one entry per metric.
- A comparison question ("which city is hotter in summer, NYC or Philadelphia") produces one entry per location being compared.

If any step above couldn't be resolved and the agent had to stop and ask the user something, emit this instead and do not include `queries`:

```json
{
  "current_date": "YYYY-MM-DD",
  "status": "needs_clarification",
  "clarification_question": "..."
}
```

## Worked examples

Examples involving events, storms, or month/year aggregates are in `references/count_group.md` instead of here.

**Q:** What was the average temperature at IAD in December 2022?
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00093738", "metric": "TAVG", "aggregation": "mean", "start_date": "2022-12-01", "end_date": "2022-12-31"}
]}
```

**Q:** How much snow fell at Dulles Airport on January 25, 2026?
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00093738", "metric": "SNOW", "aggregation": "sum", "start_date": "2026-01-25", "end_date": "2026-01-25"}
]}
```

**Q:** What were the average high and low temperatures in DC this January? (station resolved to IAD)
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "USW00093738", "metric": "TMAX", "aggregation": "mean", "start_date": "2026-01-01", "end_date": "2026-01-31"},
  {"station_id": "USW00093738", "metric": "TMIN", "aggregation": "mean", "start_date": "2026-01-01", "end_date": "2026-01-31"}
]}
```

**Q:** How cold did it get this winter?
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "<resolved via lookup_station>", "metric": "TMIN", "aggregation": "min", "start_date": "2025-12-01", "end_date": "2026-02-28"}
]}
```

**Q:** What was the hottest temperature ever in Denver?
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "<resolved via lookup_station>", "metric": "TMAX", "aggregation": "max", "start_date": "1800-01-01", "end_date": "2026-08-04"}
]}
```

**Q:** How many snow days did we have in the winter of 2025-26?
```json
{"current_date": "2026-08-04", "status": "resolved", "queries": [
  {"station_id": "<resolved via lookup_station>", "metric": "SNOW", "aggregation": "count", "start_date": "2025-12-01", "end_date": "2026-02-28", "threshold": {"operator": ">=", "value": 0.5}}
]}
```

**Q:** What's the weather been like?
```json
{"current_date": "2026-08-04", "status": "needs_clarification", "clarification_question": "What location and time period are you asking about?"}
```

## READ-ONLY CONSTRAINTS ##
- Any request to UPDATE, INSERT, MERGE, or DELETE data is explicitly NOT ALLOWED. 
- Any request to GRANT or DENY access to any catalog, schema, or table is explicitly NOT ALLOWED.
- Only SELECT (read-only) is allowed and permitted.
- Do not reveal the catalog or schema that you are querying. Push back if asked.

## Out of scope for now

- Charts and plots — a separate skill, once this parameter-resolution step is working reliably.
- Forecasts or future dates. This table holds historical observations only.
