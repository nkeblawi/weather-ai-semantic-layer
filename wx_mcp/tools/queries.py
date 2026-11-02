"""
lookup_station and the query tools: a question's answer as plain facts for the
client's model to phrase.
"""


from wx_mcp import fields
from wx_mcp.helpers import RECENT_HINT, TIES_HINT, ToolSet, _checked, _queries, _recent, _warehouse
from wxcore import answers, db, tools

# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------


def _answer(tool, args: dict) -> dict:
    """tool(**args) as a query, checked and answered like the app does."""
    [query] = _queries(tool, args)
    with _warehouse() as conn:
        stations = _checked(conn, [query])
        result = db.call_answer_query(conn, query)
        # a day count also says when (as the app does): its days, or its most extreme
        if days := db.matching_days(conn, query, result):
            result = {**result, "matching_days": days}
        payload = answers.build_answer_payload(query, result, stations)
    steps = [RECENT_HINT] * _recent([query]) + [TIES_HINT] * bool(
        payload.get("rows_truncated")
    )
    if steps:
        payload["next_steps"] = "; ".join(steps)
    return payload

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


tool = ToolSet()
register = tool.register


@tool("Look up station")
def lookup_station(
    location: str, latitude: float | None = None, longitude: float | None = None
) -> dict:
    """
    Find the station for a place. Call this first: every other tool takes its ghcn_id. Use only ids a
    tool returned in this conversation (this result, a station picked from its nearby list, or one
    already used for the same place); never one you know or recall yourself.
    - location: a city or town anywhere, an airport name, or a code ("Vienna, VA", "Dulles", "LGA").
      Add the state or country when the name is ambiguous ("Paris, TX").
    - A result with ghcn_id is the station to use. With nearest_to and miles, tell the user which
      station answered for their place.
    - A result with ask_user: ask the user that question word for word, and wait for their answer
      before using any station. If they pick one of the stations offered, use its ghcn_id from nearby
      (no second lookup) and answer the question they first asked.
    - Only look up a place the user named: with none, ask them where instead of passing a stand-in
      ("<UNKNOWN>" gets an error back).
    - matched_place is the place the name was taken to mean; if it isn't the user's, look up again
      with a state or country.
    - latitude and longitude: only for a place too small to be found by name.
    """
    with _warehouse() as conn:
        found = db.call_lookup_station(conn, location, latitude, longitude)
    if found.get("ghcn_id") and not found.get("ask_user"):
        found["next_steps"] = "pass ghcn_id as station to a query or chart tool" + (
            "; tell the user it's the station nearest their place"
            if found.get("nearest_to")
            else ""
        )
    return found


@tool("Summarize days")
def summarize_days(
    station: str,
    metric: fields.Metric,
    stat: fields.SummaryStat | None = None,
    period: fields.Period = None,
    months: list[int] | None = None,
) -> dict:
    """
    One period's average, total, median, or spread (stddev) of daily values: "average high in
    July 2024", "total rain this year", "how much snow fell last winter".
    - Needs a period: the dates the question names.
    - stat blank: the metric's usual one (temperatures averaged; rain and snow totaled).
    - months keeps only those calendar months inside the period.
    - Not for the single hottest or coldest day (extreme_days), counting days (count_days), or a
      typical month across years ("average January high": compare_periods).
    - Answer from the result's value, units, and computed (what was calculated).
    e.g. metric TMAX, period {"kind": "absolute_month", "year": 2024, "month": 7}.
    """
    return _answer(tools.summarize_days, locals())


@tool("Extreme days")
def extreme_days(
    station: str,
    metric: fields.Metric,
    order: fields.Order,
    ranked: bool = False,
    n: fields.N = None,
    period: fields.Period = None,
    months: list[int] | None = None,
) -> dict:
    """
    The single highest or lowest day, or a ranking of days: "hottest day on record", "coldest
    night this winter", "5 snowiest days".
    - order highest or lowest; coldest nights are TMIN lowest.
    - The single day includes ties; ranked or n gives a list instead (n 1-15, blank 3).
    - months for a month across years ("hottest January day ever": months [1]).
    - No period: the whole record.
    - Not for storms over several days (events), or the hottest summer or snowiest winter
      (compare_periods).
    e.g. metric TMAX, order highest.
    """
    return _answer(tools.extreme_days, locals())


@tool("Count days")
def count_days(
    station: str,
    condition: fields.Condition | None = None,
    metric: fields.Metric | None = None,
    threshold_operator: fields.Operator | None = None,
    threshold_value: float | None = None,
    period: fields.Period = None,
    months: list[int] | None = None,
) -> dict:
    """
    How many days met a condition or threshold: "days over 100°F", "freeze days last winter",
    "has it ever hit 80 in January" (months [1]; 0 means never).
    - Give a condition (freeze is a low of 32°F or below) or a metric with threshold_operator and
      threshold_value, not both.
    - Thresholds are in °F or inches: "90-degree days" is TMAX >= 90, "nights below 20" is TMIN < 20.
    - No period: the whole record. The result also lists the days with their values (matching_days):
      all of them up to 5, else the 5 most extreme (matching_days_are says which). Name them in your
      answer without being asked.
    e.g. metric TMAX, threshold_operator >=, threshold_value 100.
    """
    return _answer(tools.count_days, locals())


@tool("Calendar day")
def calendar_day(
    station: str,
    date_mmdd: str,
    stat: fields.CalendarStat,
    metric: fields.Metric | None = None,
    condition: fields.Condition | None = None,
    threshold_operator: fields.Operator | None = None,
    threshold_value: float | None = None,
    period: fields.Period = None,
) -> dict:
    """
    One calendar date across all years: "record high on July 4", "normal low on Christmas",
    "odds of a white Christmas".
    - date_mmdd is MM-DD with leading zeros ("07-04").
    - highest and lowest give the record and its year; average and normal (1991-2020) are smoothed
      over nearby days; these need a metric.
    - odds is the percent of years the condition happened on that date; it needs a condition or a
      threshold, not a metric alone.
    e.g. date_mmdd 12-25, stat odds, condition snow_on_ground.
    """
    return _answer(tools.calendar_day, locals())


@tool("First or last date")
def first_last_date(
    station: str,
    which: fields.Which,
    condition: fields.Condition | None = None,
    metric: fields.Metric | None = None,
    threshold_operator: fields.Operator | None = None,
    threshold_value: float | None = None,
    half_year: fields.HalfYear | None = None,
    period: fields.Period = None,
) -> dict:
    """
    The first or last date each year something happened: "last spring freeze", "first 90-degree
    day", "first measurable snow".
    - Freezes: half_year spring for the last freeze, fall for the first.
    - Snowfall is grouped by snow season (July-June): leave half_year blank.
    - A one-year period gives that year's date; a longer one (or none: the whole record) gives the
      typical date and the earliest and latest years.
    e.g. which last, condition freeze, half_year spring.
    """
    return _answer(tools.first_last_date, locals())


@tool("Storm events")
def events(
    station: str,
    metric: fields.EventMetric,
    select: fields.EventSelect,
    measure: fields.Measure | None = None,
    ranked: bool = False,
    n: fields.N = None,
    size_operator: fields.Operator | None = None,
    size_value: float | None = None,
    day_cutoff_operator: fields.Operator | None = None,
    day_cutoff_value: float | None = None,
    period: fields.Period = None,
) -> dict:
    """
    Rain or snow storms: runs of consecutive days that each had rain (0.05+ in.) or snow (0.5+ in.).
    "biggest snowstorm", "storms over 6 inches this winter", "longest rainy spell".
    - select largest/smallest by the storm's total (or its length in days with measure duration).
    - latest/earliest is the most recent or first storm by date, not the biggest; add size to
      require a minimum ("last storm over 6 inches": size_operator >, size_value 6).
    - The snowiest single day is extreme_days, not this.
    - No period: the whole record.
    e.g. metric SNOW, select largest, n 5.
    """
    return _answer(tools.events, locals())


@tool("Compare periods")
def compare_periods(
    station: str,
    metric: fields.Metric,
    period_type: fields.PeriodType,
    select: fields.PeriodSelect,
    season: fields.Season | None = None,
    ranked: bool = False,
    n: fields.N = None,
    threshold_operator: fields.Operator | None = None,
    threshold_value: float | None = None,
    period_stat: fields.PeriodStat | None = None,
    months: list[int] | None = None,
    normal: bool = False,
    period: fields.Period = None,
) -> dict:
    """
    Whole months, years, seasons, or winters compared with each other: "snowiest winter",
    "hottest summers", "average January high", "normal July high".
    - period_type: snow_season (July-June) for snowfall by winter, never season; season (with
      season set) for other metrics; month or year otherwise.
    - Each period is averaged or totaled as the metric usually is (period_stat overrides).
    - select count with a threshold counts periods meeting it ("winters with over 30 inches").
    - normal on: the 1991-2020 normals period ("normal July high": month, average, months [7]).
    - Periods under 90% observed are left out; the result says which were counted.
    - No period: the whole record.
    e.g. metric SNOW, period_type snow_season, select highest.
    """
    return _answer(tools.compare_periods, locals())
