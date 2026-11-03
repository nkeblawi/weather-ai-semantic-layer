"""
The tools' argument types: dropdowns whose values come from wxcore, and the
descriptions client models read.
"""

from typing import Annotated, Literal
from pydantic import Field
from wxcore import tools
from wxcore.validation import (
    CONDITIONS,
    EVENT_METRICS,
    EVENT_VALUES,
    HALF_YEARS,
    METRICS,
    OPERATORS,
    SEASONS,
)

Metric = Annotated[
    Literal[tuple(sorted(METRICS))],
    Field(description="Pick a weather metric to measure"),
]
Condition = Annotated[
    Literal[tuple(CONDITIONS)],
    Field(
        description="Pick a kind of day, or leave blank and set metric + threshold instead"
    ),
]
Operator = Annotated[
    Literal[tuple(sorted(OPERATORS))],
    Field(description="Pick how to compare with the value"),
]
SummaryStat = Annotated[
    Literal[tuple(tools.SUMMARY_STATS)],
    Field(
        description="Pick how to combine days, or leave blank for the usual (average; total for rain and snow)"
    ),
]
CalendarStat = Annotated[
    Literal[tuple(tools.CALENDAR_STATS)],
    Field(
        description="Pick record highest/lowest, average, normal (1991-2020), or odds (needs a condition or threshold)"
    ),
]
Order = Annotated[
    Literal[tuple(tools.ORDERS)],
    Field(description="Pick highest or lowest"),
]
Which = Annotated[
    Literal[tuple(tools.WHICH_DATES)],
    Field(description="Pick first or last date each year"),
]
HalfYear = Annotated[
    Literal[tuple(HALF_YEARS)],
    Field(
        description="Pick spring for a last freeze, fall for a first freeze; leave blank for snow"
    ),
]
EventMetric = Annotated[
    Literal[tuple(sorted(EVENT_METRICS))],
    Field(description="Pick PRCP for rain storms or SNOW for snowstorms"),
]
EventSelect = Annotated[
    Literal[tuple(tools.EVENT_SELECTS)],
    Field(description="Pick which events to find, or what to report about them"),
]
Measure = Annotated[
    Literal[tuple(sorted(EVENT_VALUES))],
    Field(description="Pick total amount or duration in days; leave blank for total"),
]
PeriodType = Annotated[
    Literal[tuple(sorted(tools.PERIOD_TYPES))],
    Field(description="Pick what to compare; use snow_season for snowfall by winter"),
]
PeriodSelect = Annotated[
    Literal[tuple(tools.PERIOD_SELECTS)],
    Field(description="Pick which periods to find, or what to report about them"),
]
Season = Annotated[
    Literal[tuple(sorted(SEASONS))],
    Field(description="Pick a season; needed when period_type is season"),
]
PeriodStat = Annotated[
    Literal[tuple(tools.PERIOD_STATS)],
    Field(
        description="Pick average or total per period, or leave blank for the metric's usual"
    ),
]
Location = Annotated[
    str, Field(max_length=200)  # the longest place name (97) with its state and country
]
Country = Annotated[
    str | None,
    Field(description="Enter a country name or code, such as Canada or US"),
]
RankingOf = Annotated[
    Literal[("day", "storm", *sorted(tools.PERIOD_TYPES))],
    Field(description="Pick what to rank; use snow_season for snowfall by winter"),
]
N = Annotated[
    int | None,
    Field(description="Enter any number between 1 and 15. Default is 3 if blank."),
]

Grain = Annotated[
    Literal[tuple(sorted(tools.GRAINS))],
    Field(
        description="Pick one point per day, month, year, season, or snow season (July-June, SNOW only); "
        "leave blank to pick from the span"
    ),
]
AgainstNormal = Annotated[
    Literal[tuple(sorted(tools.AGAINST_NORMAL))],
    Field(
        description="Pick overlay (next to the normal range) or anomaly (minus normal); leave blank for neither"
    ),
]
Baseline = Annotated[
    dict | None,
    Field(
        description='Normals years, e.g. {"start_year": 1981, "end_year": 2010}; leave out for 1991-2020'
    ),
]
ClimatologyBy = Annotated[
    Literal[tuple(tools.CLIMATOLOGY_BY)],
    Field(
        description="Pick one point per calendar month, season, week, or day of the year"
    ),
]
CountPer = Annotated[
    Literal[tuple(sorted(tools.COUNT_PER))],
    Field(
        description="Pick to count per year, season, snow season (July-June), or month; or average by "
        "month_of_year, season_of_year, or week_of_year"
    ),
]
Station2 = Annotated[
    str | None,
    Field(
        description="A second ghcn_id to compare, its own line; leave blank for one station"
    ),
]
Station3 = Annotated[
    str | None,
    Field(
        description="A third ghcn_id to compare, its own line; leave blank for two or fewer"
    ),
]
Metric2 = Annotated[
    Literal[tuple(sorted(METRICS))] | None,
    Field(
        description="A second metric on the same chart, its own line (e.g. TMIN with TMAX); leave blank for one"
    ),
]

DatesWhich = Annotated[
    Literal[tuple(sorted(tools.CHART_DATES))],
    Field(
        description="Pick first or last date each year, both (last spring and first fall, two lines), "
        "or growing_season (the days between them)"
    ),
]
HistogramPer = Annotated[
    Literal[tuple(sorted(tools.HISTOGRAM_UNITS))],
    Field(
        description="Pick to histogram each period's average or total; leave blank for daily values"
    ),
]
RainOrSnow = Annotated[
    Literal[tuple(sorted(EVENT_METRICS))],
    Field(description="Pick PRCP for rain or SNOW for snowfall"),
]

Period = Annotated[
    dict | None,
    Field(
        description=(
            "A kind the server resolves to dates (prefer these to working dates out yourself): "
            '{"kind": "absolute_year", "year": 2024}; '
            '{"kind": "absolute_year_range", "start_year": 2000, "end_year": 2020}; '
            '{"kind": "absolute_month", "year": 2022, "month": 12}; '
            '{"kind": "absolute_day", "year": 2026, "month": 1, "day": 25}; '
            '{"kind": "season", "season": "winter|spring|summer|fall"} for the most recently '
            'completed one, or with "year" (the year of its last month: winter 2024 is Dec 2023-Feb 2024); '
            '{"kind": "recent_month", "month": 1} for a month name with no year (the most recent one that has started); '
            '{"kind": "relative_days", "days": 30} for "the last 30 days"; '
            '{"kind": "last_years", "years": 5} for "the last 5 years": the N most recent calendar years '
            'through today, this year included ("a few" is 5, "several" or "recent" is 10; don\'t ask how many); '
            '{"kind": "weekday", "weekday": "friday"} for "last Friday" (the most recent one before today); '
            '{"kind": "snow_season", "year": 2010} for a July-June snow season, by the year it ends in; '
            '{"kind": "all_time"}. '
            'Or explicit {"start_date": "2024-01-01", "end_date": "2024-12-31"}. Leave out for the whole record.'
        )
    ),
]
