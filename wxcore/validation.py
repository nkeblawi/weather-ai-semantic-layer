"""
Query validation: the allowed values and limits a query must fit, and the
checks that run on queries before anything runs on the warehouse.

validate_queries checks a list of queries; cap_rank_limits and
coerce_series fix what can be answered instead of rejected. All three take
the list of queries itself. Nothing here calls a model or the database, or
reads the user's question text.

The allowed-value sets below mirror SKILL.md Step 6's output schema;
tests/test_guardrails.py (local-only, gitignored) fails if the two drift apart.
"""

from __future__ import annotations

from datetime import date

from wxcore.dates import ALL_TIME_START, FALL_MONTHS, SPRING_MONTHS
from wxcore.units import TEMP_METRICS, TOTAL_METRICS, natural_aggregation


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# --- Limits ---
MAX_QUERIES_PER_QUESTION = 4  # each query is a warehouse call
MAX_RANK_LIMIT = 15  # top/bottom ranks per query; answer_query clamps to the same

# Series: a monthly series longer than this becomes yearly points, which keeps
# 4 series under Altair's 5,000-row default (4 x 1,200 points)
MAX_MONTHLY_SERIES_YEARS = 100

# Series: daily points only for a span this long or shorter (a year, leap or
# not, or one snow season); longer daily requests become monthly
MAX_DAILY_SERIES_DAYS = 366
MIN_BASELINE_YEARS, MAX_BASELINE_YEARS = 10, 50

# A month, year, or season counts only with this share of its days observed (the SQL functions' coverage rule)
MIN_BUCKET_COVERAGE = 0.9


# --- Allowed values (SKILL.md Step 6, plus references/plotting.md for series) ---
METRICS = {"TAVG", "TMAX", "TMIN", "PRCP", "SNOW", "SNWD", "AWND", "HDD", "CDD"}
AGGREGATIONS = {
    "mean",
    "median",
    "sum",
    "max",
    "min",
    "stddev",
    "count",
    "latest",
    "earliest",
    "top",
    "bottom",
    "series",
    "first_date",
    "last_date",
}

# the first/last day each year (or Jul-Jun season) meeting a threshold: freeze dates, first 90-degree day
DATE_AGGREGATIONS = {"first_date", "last_date"}
DATE_UNITS = {"year", "snow_season"}
RANKING_AGGREGATIONS = {"top", "bottom"}
UNITS = {
    "day",
    "event",
    "month",
    "year",
    "snow_season",
    "season",
    "month_of_year",
    "season_of_year",
    "day_of_year",
    "week_of_year",
    "value",
}
CLIMATOLOGY_UNITS = {
    "month_of_year",
    "season_of_year",
    "day_of_year",
    "week_of_year",
}  # series only, except day_of_year (below)

# day_of_year outside a series: one calendar day's record (max/min) or normal (mean)
CALENDAR_DAY_AGGREGATIONS = {"max", "min", "mean"}
SERIES_UNITS = {
    "day",
    "month",
    "year",
    "season",
    "snow_season",
    "value",
} | CLIMATOLOGY_UNITS
DISPLAYS = {
    "overlay",
    "anomaly",
    "climate",
    "cumulative",
    "histogram",
    "difference",
    "climograph",
    "growing_season",
}

# difference: two stations' series as one A - B line; climograph: monthly temps + precip in one chart
CLIMOGRAPH_METRICS = {"TMAX", "TMIN", "TAVG", "PRCP", "SNOW"}

# histogram of per-period values (the spread of annual snowfall); unit value is always one
HISTOGRAM_UNITS = {"month", "year", "season", "snow_season"}

# climate: NWS-style daily graph (normals + records); cumulative: running total
DAILY_DISPLAYS = {"climate": TEMP_METRICS, "cumulative": TOTAL_METRICS}
SEASONS = {"winter", "spring", "summer", "fall"}
OPERATORS = {">", ">=", "<", "<=", "="}
EVENT_METRICS = TOTAL_METRICS
EVENT_ONLY_AGGREGATIONS = {"latest", "earliest"}
EVENT_VALUES = {"total", "duration"}
PERIOD_AGGREGATIONS = {"mean", "sum"}

# a series bucket can also count the days meeting its threshold (90F+ days per year);
# any: 1 if a day in the bucket meets the threshold -- a climatology of it is the share of years;
# first/last_date: the first/last such day. All three need a threshold.
THRESHOLD_PERIOD_AGGREGATIONS = {"count", "any"} | DATE_AGGREGATIONS
SERIES_PERIOD_AGGREGATIONS = PERIOD_AGGREGATIONS | THRESHOLD_PERIOD_AGGREGATIONS
OUTPUT_UNITS = {"C", "F"}

# Halves of the year a first/last date is searched in: the last spring freeze
# is in spring's half, the first fall freeze in fall's (wxcore.defaults turns
# half_year into month_filter).
HALF_YEARS = {"spring": SPRING_MONTHS, "fall": FALL_MONTHS}
# Named day conditions: name -> (metric, operator, value). A query with a
# condition gets its metric and threshold from it (wxcore.defaults).
# Freeze is a low at or below 32F (0C).
CONDITIONS = {
    "snow_day": ("SNOW", ">=", 0.5),
    "measurable_snow": ("SNOW", ">=", 0.1),
    "rain_day": ("PRCP", ">=", 0.05),
    "measurable_rain": ("PRCP", ">=", 0.01),
    "dry_day": ("PRCP", "=", 0),
    "freeze": ("TMIN", "<=", 32),
    "hard_freeze": ("TMIN", "<=", 28),
    "snow_on_ground": ("SNWD", ">=", 1),
}

# Plot modes, which say whether queries may or must be series: PLOT_EXPLICIT
# (a chart request: all series, or a ranking drawn as bars), PLOT_INHERITED
# (either kind, e.g. a follow-up to a chart), PLOT_OFF (no series).
PLOT_EXPLICIT, PLOT_INHERITED, PLOT_OFF = "plot", "plot_inherited", None
MIN_DATE = date.fromisoformat(ALL_TIME_START)  # the start of every record


# --- Query validation (before the warehouse) ---
DAILY_LIMIT_NOTE = (
    "Daily points are limited to a year or less, so this shows monthly values."
)


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

# --- Query validation (before the warehouse) ---


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_threshold(name: str, threshold, problems: list[str]) -> None:
    if not isinstance(threshold, dict):
        problems.append(f"{name} is not an object")
        return
    if threshold.get("operator") not in OPERATORS:
        problems.append(f"{name}.operator {threshold.get('operator')!r} not allowed")
    if not _is_number(threshold.get("value")):
        problems.append(f"{name}.value {threshold.get('value')!r} is not a number")


def _parse_date(value) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _check_half_year(q, aggregation) -> list[str]:
    half = q["half_year"]
    if not isinstance(half, str) or half not in HALF_YEARS:
        return [f"half_year {half!r} not allowed"]
    problems = []
    if aggregation not in DATE_AGGREGATIONS and q.get("period_aggregation") not in DATE_AGGREGATIONS:
        problems.append("half_year only applies to a first_date or last_date")
    if q.get("unit", "day") != "year":
        problems.append(f"half_year needs unit 'year', got {q.get('unit', 'day')!r}")
    if q.get("month_filter") != HALF_YEARS[half]:
        problems.append(f"half_year {half!r} means month_filter {HALF_YEARS[half]}, got {q.get('month_filter')!r}")
    return problems


def _check_condition(q, metric, unit) -> list[str]:
    name = q["condition"]
    if not isinstance(name, str) or name not in CONDITIONS:
        return [f"condition {name!r} not allowed"]
    want_metric, operator, value = CONDITIONS[name]
    problems = []
    if metric != want_metric:
        problems.append(
            f"condition {name!r} is for {want_metric}, got metric {metric!r}"
        )
    if unit == "event":
        problems.append(f"condition {name!r} doesn't apply to unit 'event'")
    elif q.get("threshold") != {"operator": operator, "value": value}:
        problems.append(
            f"condition {name!r} means threshold {operator} {value}, got {q.get('threshold')!r}"
        )
    return problems


def _validate_query(q, station_info: dict, today: date) -> list[str]:
    if not isinstance(q, dict):
        return ["query is not an object"]
    problems: list[str] = []

    # Must be a station lookup_station returned (station_info), not an ID the
    # model made up.
    if q.get("station_id") not in station_info:
        problems.append(
            f"station_id {q.get('station_id')!r} wasn't returned by lookup_station"
        )

    metric = q.get("metric")
    aggregation = q.get("aggregation")
    unit = q.get("unit", "day")
    if metric not in METRICS:
        problems.append(f"metric {metric!r} not allowed")
    if "condition" in q:
        problems += _check_condition(q, metric, unit)
    if "normal" in q and q["normal"] is not True:
        problems.append(f"normal must be true or omitted, got {q['normal']!r}")
    if aggregation not in AGGREGATIONS:
        problems.append(f"aggregation {aggregation!r} not allowed")
    if unit not in UNITS:
        problems.append(f"unit {unit!r} not allowed")
    if aggregation in EVENT_ONLY_AGGREGATIONS and unit != "event":
        problems.append(f"aggregation {aggregation!r} requires unit 'event'")

    if unit == "event":
        if metric not in EVENT_METRICS:
            problems.append(f"unit 'event' requires PRCP or SNOW, got {metric!r}")
        if "event_day_threshold" not in q:
            problems.append("unit 'event' requires event_day_threshold")
        if q.get("event_value", "total") not in EVENT_VALUES:
            problems.append(f"event_value {q.get('event_value')!r} not allowed")
    if "limit" in q:
        lim = q["limit"]
        if aggregation not in RANKING_AGGREGATIONS:
            problems.append(f"limit only applies to top/bottom, not {aggregation!r}")
        elif not (
            isinstance(lim, int)
            and not isinstance(lim, bool)
            and 1 <= lim <= MAX_RANK_LIMIT
        ):
            problems.append(f"limit {lim!r} must be an integer 1-{MAX_RANK_LIMIT}")
    if unit == "season" and q.get("season") not in SEASONS:
        problems.append(
            f"unit 'season' requires season winter, spring, summer, or fall, got {q.get('season')!r}"
        )
    if "calendar_day" in q and (unit != "day_of_year" or aggregation == "series"):
        problems.append("calendar_day only applies to a day_of_year record or normal")
    if aggregation in DATE_AGGREGATIONS:
        if unit not in DATE_UNITS:
            problems.append(
                f"aggregation {aggregation!r} needs unit year or snow_season"
            )
        if "threshold" not in q:
            problems.append(f"aggregation {aggregation!r} requires a threshold")
    if aggregation == "series":
        _validate_series(q, unit, today, problems)
    elif unit == "day_of_year":
        if aggregation not in CALENDAR_DAY_AGGREGATIONS:
            problems.append(
                f"a calendar-day query takes max, min, or mean, not {aggregation!r}"
            )
        pa = q.get("period_aggregation")
        if pa is not None and not (
            pa == "any" and aggregation == "mean" and "threshold" in q
        ):
            problems.append(
                "a calendar-day query takes period_aggregation only as any (odds), with mean and a threshold"
            )
        cd = q.get("calendar_day")
        try:
            date.fromisoformat(f"2000-{cd}")  # a leap year, so 02-29 is valid
        except (TypeError, ValueError):
            problems.append(f"calendar_day {cd!r} must be MM-DD")
    elif unit in CLIMATOLOGY_UNITS:
        problems.append(f"unit {unit!r} only applies to series")
    else:
        for name in ("trend", "baseline", "display"):
            if name in q:
                problems.append(f"{name} only applies to series")
    if (
        unit
        in {
            "month",
            "year",
            "snow_season",
            "season",
            "month_of_year",
            "season_of_year",
            "week_of_year",
        }
        and aggregation not in DATE_AGGREGATIONS
    ):
        allowed = (
            SERIES_PERIOD_AGGREGATIONS
            if aggregation == "series"
            else PERIOD_AGGREGATIONS
        )
        if q.get("period_aggregation") not in allowed:
            problems.append(
                f"unit {unit!r} requires period_aggregation {' or '.join(sorted(allowed))}"
            )
    # a count series may use the Jul-Jun year for any metric (freeze days per cold season)
    if (
        unit == "snow_season"
        and metric != "SNOW"
        and not (aggregation == "series" and q.get("period_aggregation") == "count")
    ):
        problems.append(f"unit 'snow_season' requires SNOW, got {metric!r}")
    # a winter's snow is grouped July-June, so an October or March storm stays in its winter
    if unit == "season" and metric == "SNOW":
        problems.append(
            "snowfall by winter or season uses unit 'snow_season' (July-June), not 'season'"
        )
    if "half_year" in q:
        problems += _check_half_year(q, aggregation)

    for name in ("threshold", "event_day_threshold"):
        if name in q:
            _check_threshold(name, q[name], problems)
    if "output_unit" in q and q["output_unit"] not in OUTPUT_UNITS:
        problems.append(f"output_unit {q['output_unit']!r} not allowed")
    if "month_filter" in q:
        mf = q["month_filter"]
        if not (
            isinstance(mf, list)
            and mf
            and all(isinstance(m, int) and 1 <= m <= 12 for m in mf)
        ):
            problems.append(f"month_filter {mf!r} must be a list of months 1-12")
        # query_by_period ignores it for any other unit, so e.g. "hottest
        # summers" as unit year + [6, 7, 8] would silently rank whole years
        # unit day: daily rows in those months across years ("the hottest
        # January day"); not for a daily series, which query_series doesn't filter
        dates = (
            aggregation in DATE_AGGREGATIONS
            or q.get("period_aggregation") in DATE_AGGREGATIONS
        )
        if (
            unit not in ("month", "value")
            and not (unit == "day" and aggregation != "series")
            and not (dates and unit == "year")
        ):
            problems.append(
                f"month_filter only applies to unit 'month' or 'day', not {unit!r}"
            )

    start, end = _parse_date(q.get("start_date")), _parse_date(q.get("end_date"))
    if start is None or end is None:
        problems.append("start_date/end_date must be YYYY-MM-DD")
    else:
        if start > end:
            problems.append("start_date is after end_date")
        if start < MIN_DATE:
            problems.append(f"start_date before {MIN_DATE}")
        # Only a range that hasn't started yet is a forecast. A range already
        # under way ("October 2026", "this year") legitimately ends in the
        # future; the query just finds observations up to today.
        if start > today:
            problems.append("start_date is in the future")
    return problems


def _validate_series(q: dict, unit: str, today: date, problems: list[str]) -> None:
    """Fields only a series (a chart query, references/plotting.md) may carry."""
    if unit not in SERIES_UNITS:
        problems.append(f"series unit {unit!r} not allowed")
    if unit == "day":
        start, end = _parse_date(q.get("start_date")), _parse_date(q.get("end_date"))
        if start and end and (end - start).days > MAX_DAILY_SERIES_DAYS:
            problems.append(f"daily series span more than {MAX_DAILY_SERIES_DAYS} days")
        # a trend within a year isn't one
        if "trend" in q:
            problems.append("trend doesn't apply to a daily series")
        if "period_aggregation" in q:
            problems.append("a daily series has no period_aggregation")
    elif unit == "value":
        for name in ("period_aggregation", "baseline", "display", "trend", "threshold"):
            if name in q:
                problems.append(f"{name} doesn't apply to a histogram of daily values")
    elif (
        q.get("period_aggregation") in THRESHOLD_PERIOD_AGGREGATIONS
        and "threshold" not in q
    ):
        problems.append(
            f"period_aggregation {q['period_aggregation']} requires a threshold"
        )
    if (
        "threshold" in q
        and q.get("period_aggregation") not in THRESHOLD_PERIOD_AGGREGATIONS
    ):
        problems.append(
            "a series threshold requires period_aggregation count, any, first_date, or last_date"
        )
    if q.get("period_aggregation") in DATE_AGGREGATIONS and unit not in DATE_UNITS:
        problems.append(f"{q['period_aggregation']} needs unit year or snow_season")
    if q.get("period_aggregation") == "any" and unit not in CLIMATOLOGY_UNITS:
        problems.append("period_aggregation any (odds) needs a climatology unit")
    if "trend" in q:
        if q["trend"] is not True:
            problems.append(f"trend {q['trend']!r} must be true or omitted")
        elif unit in CLIMATOLOGY_UNITS:
            problems.append("trend doesn't apply to a climatology")
    if "baseline" in q:
        b = q["baseline"]
        start, end = (
            (b.get("start_year"), b.get("end_year"))
            if isinstance(b, dict)
            else (None, None)
        )
        if (
            not (isinstance(start, int) and isinstance(end, int))
            or isinstance(start, bool)
            or isinstance(end, bool)
        ):
            problems.append(f"baseline {b!r} needs integer start_year and end_year")
        else:
            years = end - start + 1
            if not MIN_BASELINE_YEARS <= years <= MAX_BASELINE_YEARS:
                problems.append(
                    f"baseline is {years} years ({MIN_BASELINE_YEARS}-{MAX_BASELINE_YEARS} allowed)"
                )
            if end >= today.year:
                problems.append(f"baseline must end before {today.year}")
        if unit in CLIMATOLOGY_UNITS:
            problems.append(
                "baseline doesn't apply to a climatology (its dates are the period)"
            )
    if "display" in q:
        if q["display"] not in DISPLAYS:
            problems.append(f"display {q['display']!r} not allowed")
        elif q["display"] == "growing_season":
            if (
                q.get("period_aggregation") not in DATE_AGGREGATIONS
                or q.get("metric") != "TMIN"
            ):
                problems.append(
                    "display 'growing_season' pairs TMIN last_date (spring) and first_date (fall) series"
                )
        elif q["display"] == "difference":
            if "baseline" in q or unit == "value":
                problems.append(
                    "display 'difference' compares two series as they are: no baseline or histogram"
                )
        elif q["display"] == "climograph":
            if unit != "month_of_year" or q.get("metric") not in CLIMOGRAPH_METRICS:
                problems.append(
                    "display 'climograph' is monthly climatologies (month_of_year) of temperature and PRCP/SNOW"
                )
        elif q["display"] == "histogram":
            if unit not in HISTOGRAM_UNITS:
                problems.append(
                    f"display 'histogram' needs unit month, year, season, or snow_season, not {unit!r}"
                )
            for name in ("baseline", "trend"):
                if name in q:
                    problems.append(f"{name} doesn't apply to a histogram")
        elif q["display"] in DAILY_DISPLAYS:
            if unit != "day":
                problems.append(f"display {q['display']!r} needs unit day")
            if q.get("metric") not in DAILY_DISPLAYS[q["display"]]:
                problems.append(
                    f"display {q['display']!r} doesn't apply to {q.get('metric')!r}"
                )
            if q["display"] == "climate" and "baseline" not in q:
                problems.append("display 'climate' needs a baseline")
        elif "baseline" not in q:
            problems.append("display needs a baseline")


def _cross_query_displays(queries: list[dict]) -> list[str]:
    """Displays that need a set of queries: a difference pairs two, a climograph needs both kinds of line."""
    problems = []
    diff = [q for q in queries if q.get("display") == "difference"]
    if diff and (
        len(diff) != 2
        or len({q.get("metric") for q in diff}) != 1
        or len({q.get("unit") for q in diff}) != 1
        or len({q.get("station_id") for q in diff}) != 2
    ):
        problems.append(
            "display 'difference' needs exactly two queries: same metric and unit, two stations"
        )
    grow = [q for q in queries if q.get("display") == "growing_season"]
    if grow and sorted(q.get("period_aggregation") for q in grow) != [
        "first_date",
        "last_date",
    ]:
        problems.append(
            "display 'growing_season' needs a last_date (spring) and a first_date (fall) query"
        )
    clim = [q for q in queries if q.get("display") == "climograph"]
    if clim and not (
        {q.get("metric") for q in clim} & TEMP_METRICS
        and {q.get("metric") for q in clim} & TOTAL_METRICS
    ):
        problems.append(
            "display 'climograph' needs a temperature query and a PRCP/SNOW query"
        )
    return problems


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------

# --- Query validation (before the warehouse) ---


def cap_rank_limits(queries) -> None:
    """
    Trims a top/bottom limit over MAX_RANK_LIMIT down to it, in place, and
    keeps what was asked for as requested_limit so the answer can say so
    ("top 20" -> 15 ranks). Runs before validate_queries, so an over-cap
    limit is answered instead of rejected; anything else odd about limit
    (not an integer, below 1) is still left for validation to reject.
    A limit of 1 on max/min ("which year was wettest" asked as max with
    limit 1) is dropped: max/min already is the single top value.
    """
    for q in queries or []:
        if not isinstance(q, dict):
            continue
        if q.get("aggregation") in {"max", "min"} and q.get("limit") == 1:
            del q["limit"]
        if q.get("aggregation") not in RANKING_AGGREGATIONS:
            continue
        lim = q.get("limit")
        if isinstance(lim, int) and not isinstance(lim, bool) and lim > MAX_RANK_LIMIT:
            q["requested_limit"] = lim
            q["limit"] = MAX_RANK_LIMIT


def series_span(q: dict) -> tuple[date | None, date | None, bool]:
    """(start, end, long_span): the query's dates, and whether they span more than daily points allow."""
    start, end = _parse_date(q.get("start_date")), _parse_date(q.get("end_date"))
    return (
        start,
        end,
        bool(start and end and (end - start).days > MAX_DAILY_SERIES_DAYS),
    )


def coerce_series(queries) -> None:
    """
    Fixes series queries that ask for more than charts allow, in place, before
    validation -- a chart instead of an error, like cap_rank_limits. Daily
    points over more than MAX_DAILY_SERIES_DAYS become monthly; a monthly
    series over MAX_MONTHLY_SERIES_YEARS becomes yearly. Each change leaves a
    note under the chart (notes). Reads the query parameters only, never the
    question's wording.
    """
    for q in queries or []:
        if not isinstance(q, dict) or q.get("aggregation") != "series":
            continue
        start, end, long_span = series_span(q)
        if q.get("unit", "day") == "day" and long_span:
            q["unit"] = "month"
            # monthly buckets need one; a daily mean is the closest reading
            q.setdefault(
                "period_aggregation",
                natural_aggregation(q.get("metric")) or "mean",
            )
            q.setdefault("notes", []).append(DAILY_LIMIT_NOTE)
        elif (
            q.get("unit", "day") == "day"
            and q.get("period_aggregation") in PERIOD_AGGREGATIONS
        ):
            # a day is its own value; mean/sum of one day is a no-op
            del q["period_aggregation"]
        if (
            q.get("unit") == "month"
            and start
            and end
            and (end.year - start.year + 1) > MAX_MONTHLY_SERIES_YEARS
        ):
            q["unit"] = "year"
            q.setdefault("notes", []).append(
                f"Monthly points are limited to {MAX_MONTHLY_SERIES_YEARS} years, so this shows yearly values."
            )


def validate_queries(
    queries,
    station_info: dict,
    today: date | None = None,
    *,
    plot_mode: str | None = PLOT_OFF,
) -> list[str]:
    """
    Problems with a list of queries, or [] if it's safe to run them.
    station_info holds the stations lookup_station returned; plot_mode is
    one of the plot modes above.
    """
    today = today or date.today()
    if not isinstance(queries, list) or not queries:
        return ["resolved output has no queries"]
    if len(queries) > MAX_QUERIES_PER_QUESTION:
        return [f"{len(queries)} queries (max {MAX_QUERIES_PER_QUESTION})"]
    problems = [p for q in queries for p in _validate_query(q, station_info, today)]
    series = [isinstance(q, dict) and q.get("aggregation") == "series" for q in queries]
    rankings = [
        isinstance(q, dict) and q.get("aggregation") in RANKING_AGGREGATIONS
        for q in queries
    ]
    if plot_mode == PLOT_OFF and any(series):
        problems.append("series outside plot mode")
    # a chart of a ranking ("the 10 biggest snowstorms") draws its rows as bars
    elif plot_mode == PLOT_EXPLICIT and not (all(series) or all(rankings)):
        problems.append("/plot answered without a series")
    elif any(series) and not all(series):
        problems.append("series mixed with single-value queries")
    problems += _cross_query_displays([q for q in queries if isinstance(q, dict)])
    return problems
