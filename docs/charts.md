# Charts

Command: `/plot`

Start a question with the `/plot` command. A chart question without it ("average high
and low for each month at Dulles") gets a **Plot it** button that resends
it as `/plot`, and a follow-up to a chart ("now do Reagan National") stays
a chart unless it asks for a single value. `wxcore/series.py` turns the SQL
result into chart data (unit conversion, normals, trend fits) and
`wxcore/charts.py` holds the Altair templates.

## Chart types

| Chart | Example |
|---|---|
| Time series, monthly / yearly / seasonal | `/plot summer highs at Reagan National with a trend line` |
| Counts of days per period | `/plot the number of 90-degree days per year at Dulles` |
| Climatology by month, season, or calendar day | `/plot average high and low for each month at Dulles` |
| Against normal (overlay or departure) | `/plot monthly temperatures at Dulles in 2024 against normal` |
| Daily values, a year or less | `/plot daily rainfall at Reagan National in 2023` |
| Daily climate graph (normals and records) | `/plot a climate graph for Dulles in 2024` |
| Running totals, several seasons overlaid | `/plot accumulated snowfall at Dulles in 2024-25 vs 2009-10` |
| Histograms (daily or per-period values) | `/plot a histogram of July high temperatures at Dulles` |
| Odds by month, week, season, or day | `/plot the chance of measurable snow by week at Dulles` |
| Ranked bars | `/plot the 10 biggest snowstorms at Dulles` |
| Station difference | `/plot how much colder Dulles lows are than Reagan National's, by year` |
| Climograph | `/plot a climograph for Reagan National` |
| Dates by year (freezes, first 90°F day), growing season | `/plot the last spring and first fall freeze at Dulles each year with a trend line` |

## Normals, records, and limits

- **Normals** are computed from the station's own observations, 1991-2020
  by default (10-50 years allowed). Daily normals are smoothed over +/- 7
  days. **Records** cover the whole station record.
- **Limits**: daily points only over a year or less, monthly points up to
  100 years (yearly beyond), and at most 4 series per question. Longer
  requests are reduced, with a note under the chart.
- Charts with 2+ series have a **clickable legend** to hide or show each
  one; charts with many points offer a CSV download.

## Single values

Single values use the same data without a chart: "the record high for July
4", "the normal low on Christmas" (normal = 1991-2020; "average" = the whole
record), "the odds of a white Christmas", "the last spring freeze in 2025", "when the first
measurable snow usually comes".
