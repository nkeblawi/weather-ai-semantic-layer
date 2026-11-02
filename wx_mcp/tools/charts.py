"""
The chart tools: the app's chart as a PNG, the numbers the client's model may
quote, and a CSV link when the chart has many points.
"""

import vl_convert
from mcp.server.mcpserver import Image
from mcp.server.mcpserver.exceptions import ToolError
from wx_mcp import fields
from wx_mcp.helpers import RECENT_HINT, TIES_HINT, ToolSet, _checked, _queries, _recent, _warehouse
from wx_mcp.resources import _csv_link
from wxcore import answers, charts, db, series, tools
from wxcore.validation import PLOT_EXPLICIT

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# chart config
PNG_WIDTH = 1080  # the app stretches charts to the page; a PNG needs a width
CSV_POINTS = 24  # charts with this many points or more get a CSV (the app's button needs more than 24)
LEGEND_NOTE = "Click a legend item to hide or show it."  # wxcore's note; a PNG has nothing to click
CSV_HINT = "tell the user the chart's data is available as a CSV if they want it"
NO_DATA = (
    "no observations for this station and period: check the period is within the station's record, "
    "or ask the user about a nearby station from lookup_station"
)


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------


def _png(chart: dict) -> bytes:
    """The app's chart (charts.chart_spec) as a PNG, on white at PNG_WIDTH."""
    spec = charts.chart_spec(chart)
    spec.update(width=PNG_WIDTH, background="white")
    return vl_convert.vegalite_to_png(spec, scale=2)


def _iata(conn, ids: list[str]) -> dict[str, str]:
    """Each station's IATA code from analytics.stations (fill_station_info doesn't read it)."""
    params = {f"id{i}": sid for i, sid in enumerate(ids)}
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT ghcn_id, iata FROM {db.catalog()}.analytics.stations "
            f"WHERE ghcn_id IN ({', '.join(':' + k for k in params)})",
            parameters=params,
        )
        return {ghcn_id: iata for ghcn_id, iata in cur.fetchall() if iata}


def _chart(tool, args: dict) -> list:
    """tool(**args)'s series queries, answered and drawn like the app's /plot turn."""
    tool_name = tool.__name__
    # station_2/3 and metric_2 (single fields, so no JSON lists to type) become wxcore's lists
    for name, extra in (
        ("station", ("station_2", "station_3")),
        ("metric", ("metric_2",)),
    ):
        if more := [v for v in (args.pop(k, None) for k in extra) if v]:
            args[name] = [args[name], *more]
    queries = _queries(tool, args)
    with _warehouse() as conn:
        stations = _checked(conn, queries, PLOT_EXPLICIT)
        # e.g. IAD_DCA_chart_over_time: the stations in line order (ghcn_id if no IATA code), then the tool
        ids = list(dict.fromkeys(q["station_id"] for q in queries))
        codes = _iata(conn, ids)
        prefix = "_".join([*(codes.get(i, i) for i in ids), tool_name])
        series_list = []
        for entry in queries:
            base_entry = series.baseline_entry(entry)
            rec_entry = series.records_entry(entry)
            series_list.append(
                series.build_series(
                    entry,
                    db.call_answer_query(conn, entry),
                    db.call_answer_query(conn, base_entry) if base_entry else None,
                    stations,
                    records_result=(
                        db.call_answer_query(conn, rec_entry) if rec_entry else None
                    ),
                )
            )
    drawn = series.group_charts(series_list)
    if not drawn:
        raise ToolError(NO_DATA)
    links = [
        _csv_link(prefix, n, c)
        for n, c in enumerate(drawn, 1)
        if sum(len(s["points"]) for s in c["series"]) >= CSV_POINTS
    ]
    result = {"summaries": series.phrasing_summaries(series_list)}
    # the captions the app shows under a chart, less the one for its clickable legend
    notes = dict.fromkeys(
        note for c in drawn for note in c["notes"] if note != LEGEND_NOTE
    )
    if notes:
        result["notes"] = list(notes)
    if steps := [RECENT_HINT] * _recent(queries) + [CSV_HINT] * bool(links):
        result["next_steps"] = "; ".join(steps)
    return [*(Image(data=_png(c), format="png") for c in drawn), result, *links]


def _ranking_chart(tool, args: dict) -> list:
    """tool(**args)'s ranked query, answered and drawn as bars like the app's /plot of a ranking."""
    [query] = _queries(tool, args)
    with _warehouse() as conn:
        stations = _checked(conn, [query], PLOT_EXPLICIT)
        payload = answers.build_answer_payload(
            query, db.call_answer_query(conn, query), stations
        )
    table = answers.ranking_table(payload)
    if not table["rows"]:
        raise ToolError(NO_DATA)
    chart = answers.ranking_chart(payload, table)
    if chart["notes"]:
        payload["notes"] = chart["notes"]
    steps = [RECENT_HINT] * _recent([query]) + [TIES_HINT] * bool(
        payload.get("rows_truncated")
    )
    if steps:
        payload["next_steps"] = "; ".join(steps)
    return [Image(data=_png(chart), format="png"), payload]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


tool = ToolSet()
register = tool.register


@tool("Chart over time")
def chart_over_time(
    station: str,
    metric: fields.Metric,
    station_2: fields.Station2 = None,
    station_3: fields.Station3 = None,
    metric_2: fields.Metric2 = None,
    grain: fields.Grain | None = None,
    season: fields.Season | None = None,
    stat: fields.PeriodStat | None = None,
    trend: bool = False,
    against_normal: fields.AgainstNormal | None = None,
    baseline: fields.Baseline = None,
    months: list[int] | None = None,
    period: fields.Period = None,
):
    """
    A chart of values over time: "plot July highs since 1990", "chart yearly snowfall", "graph this
    year's temperatures against normal".
    - One point per grain; blank picks from the span (daily up to a year, monthly up to 30 years,
      else yearly). season (with or without grain season) gives one season across years.
    - station_2 / station_3 compare stations, metric_2 adds a metric: one line each (at most 4
      lines: stations x metrics).
    - trend adds a trend line; against_normal overlay or anomaly compares with the 1991-2020
      normals (or baseline).
    - Returns the chart as a PNG: show it to the user. Describe it from summaries, never guessing
      at points. A CSV link, when present, is for the user to fetch; don't read it unless they ask
      for the data.
    e.g. metric TMAX, grain year, trend true.
    """
    return _chart(tools.chart_over_time, locals())


@tool("Chart typical year")
def chart_climatology(
    station: str,
    metric: fields.Metric,
    by: fields.ClimatologyBy,
    station_2: fields.Station2 = None,
    station_3: fields.Station3 = None,
    metric_2: fields.Metric2 = None,
    stat: fields.PeriodStat | None = None,
    normal: bool = False,
    months: list[int] | None = None,
    period: fields.Period = None,
):
    """
    A chart of the typical year: "plot average highs by month", "chart normal monthly rainfall",
    "graph the typical temperature through the year".
    - One point per calendar month, season, week, or day of the year (day: 366 smoothed points).
    - Over the whole record, the period, or with normal on the 1991-2020 normals (not both normal
      and a period).
    - station_2 / station_3 compare stations, metric_2 adds a metric: one line each (at most 4
      lines: stations x metrics).
    - Returns the chart as a PNG: show it to the user. Describe it from summaries, never guessing
      at points. A CSV link, when present, is for the user to fetch; don't read it unless they ask
      for the data.
    e.g. metric TMAX, metric_2 TMIN, by month, normal true.
    """
    return _chart(tools.chart_climatology, locals())


@tool("Chart day counts")
def chart_count_days(
    station: str,
    per: fields.CountPer,
    condition: fields.Condition | None = None,
    metric: fields.Metric | None = None,
    threshold_operator: fields.Operator | None = None,
    threshold_value: float | None = None,
    season: fields.Season | None = None,
    trend: bool = False,
    against_normal: fields.AgainstNormal | None = None,
    baseline: fields.Baseline = None,
    period: fields.Period = None,
):
    """
    A chart of how many days met a condition or threshold: "plot 90-degree days per year",
    "chart freeze days each winter", "graph snow days by month".
    - Give a condition or a metric with threshold_operator and threshold_value, not both.
      Thresholds are in °F or inches: "90-degree days" is TMAX >= 90.
    - per year, season (with season set), snow_season (July-June), or month counts each period;
      month_of_year, season_of_year, or week_of_year averages the count over the years.
    - trend adds a trend line; against_normal compares with the 1991-2020 normals (or baseline).
    - One station only. No period: the whole record.
    - Returns the chart as a PNG: show it to the user. Describe it from summaries, never guessing
      at points. A CSV link, when present, is for the user to fetch; don't read it unless they ask
      for the data.
    e.g. per year, metric TMAX, threshold_operator >=, threshold_value 90, trend true.
    """
    return _chart(tools.chart_count_days, locals())


@tool("Chart odds")
def chart_odds(
    station: str,
    by: fields.ClimatologyBy,
    condition: fields.Condition | None = None,
    metric: fields.Metric | None = None,
    threshold_operator: fields.Operator | None = None,
    threshold_value: float | None = None,
    period: fields.Period = None,
):
    """
    A chart of the percent of years with at least one day meeting a condition or threshold: "plot
    the chance of measurable snow by week", "chart the odds of a 90-degree day by month".
    - One point per calendar month, season, week, or day of the year.
    - Give a condition or a metric with threshold_operator and threshold_value, not both.
    - One station only. No period: the whole record.
    - Returns the chart as a PNG: show it to the user. Describe it from summaries, never guessing
      at points. A CSV link, when present, is for the user to fetch; don't read it unless they ask
      for the data.
    e.g. by week, condition measurable_snow.
    """
    return _chart(tools.chart_odds, locals())


@tool("Chart dates by year")
def chart_dates_by_year(
    station: str,
    which: fields.DatesWhich,
    condition: fields.Condition | None = None,
    metric: fields.Metric | None = None,
    threshold_operator: fields.Operator | None = None,
    threshold_value: float | None = None,
    half_year: fields.HalfYear | None = None,
    trend: bool = False,
    period: fields.Period = None,
):
    """
    A chart of the first or last date something happened each year: "plot the last spring freeze
    each year", "chart first snow dates", "graph the growing season length".
    - which first or last draws one line; both draws the last spring and first fall dates;
      growing_season draws the days between them (a freeze unless you give another condition).
    - Give a condition or a metric with threshold_operator and threshold_value, not both.
    - Freezes with first or last: half_year spring for the last freeze, fall for the first.
      Snowfall is grouped by snow season (July-June): leave half_year blank.
    - trend adds a trend line. One station only. No period: the whole record.
    - Returns the chart as a PNG: show it to the user. Describe it from summaries, never guessing
      at points. A CSV link, when present, is for the user to fetch; don't read it unless they ask
      for the data.
    e.g. which growing_season, trend true.
    """
    return _chart(tools.chart_dates_by_year, locals())


@tool("Chart histogram")
def chart_histogram(
    station: str,
    metric: fields.Metric,
    per: fields.HistogramPer | None = None,
    season: fields.Season | None = None,
    stat: fields.PeriodStat | None = None,
    months: list[int] | None = None,
    period: fields.Period = None,
):
    """
    A histogram (distribution) of values: "plot the distribution of daily highs", "histogram of
    summer average highs", "show the spread of yearly snowfall".
    - per blank: daily values. per year, season (with season set), snow_season, or month: each
      period's average or total (stat overrides the metric's usual one).
    - season and stat need per. months keeps only those calendar months.
    - One station only. No period: the whole record.
    - Returns the chart as a PNG: show it to the user. Describe it from summaries, never guessing
      at points. A CSV link, when present, is for the user to fetch; don't read it unless they ask
      for the data.
    e.g. metric TMAX, months [7].
    """
    return _chart(tools.chart_histogram, locals())


@tool("Chart climate graph")
def chart_climate_graph(
    station: str, baseline: fields.Baseline = None, period: fields.Period = None
):
    """
    An NWS-style daily climate graph: daily highs and lows over the normal and record ranges:
    "plot a climate graph for Dulles in 2024", "show this year against normal and records".
    - A period of up to a year; no period: this year to date.
    - One station only. Normals are 1991-2020 unless baseline is given.
    - Returns the chart as a PNG: show it to the user. Describe it from summaries, never guessing
      at points. A CSV link, when present, is for the user to fetch; don't read it unless they ask
      for the data.
    e.g. period {"kind": "absolute_year", "year": 2024}.
    """
    return _chart(tools.chart_climate_graph, locals())


@tool("Chart running total")
def chart_running_total(
    station: str,
    metric: fields.RainOrSnow,
    period: fields.Period = None,
    period_2: fields.Period = None,
    period_3: fields.Period = None,
    period_4: fields.Period = None,
    against_normal: bool = False,
    baseline: fields.Baseline = None,
):
    """
    A chart of rain or snow accumulating day by day: "plot accumulated snowfall this winter",
    "compare 2009-10 snowfall with 2024-25", "year-to-date rain against normal".
    - period_2 to period_4 add periods of up to a year, one line each, lined up by day.
    - No period: this year (or snow season for SNOW) to date.
    - against_normal adds the normal accumulation (1991-2020, or baseline). One station only.
    - Returns the chart as a PNG: show it to the user. Describe it from summaries, never guessing
      at points. A CSV link, when present, is for the user to fetch; don't read it unless they ask
      for the data.
    e.g. metric SNOW, period {"kind": "snow_season", "year": 2010}, period_2 {"kind": "snow_season", "year": 2025}.
    """
    return _chart(
        tools.chart_running_total,
        {
            "station": station,
            "metric": metric,
            "periods": [p for p in (period, period_2, period_3, period_4) if p],
            "against_normal": against_normal,
            "baseline": baseline,
        },
    )


@tool("Chart station difference")
def chart_station_difference(
    station: str,
    other_station: str,
    metric: fields.Metric,
    grain: fields.Grain | None = None,
    season: fields.Season | None = None,
    stat: fields.PeriodStat | None = None,
    trend: bool = False,
    period: fields.Period = None,
):
    """
    A chart of one station's values minus another's over time: "plot how much warmer Reagan is than
    Dulles", "chart the snowfall difference between two cities".
    - One line around zero: station minus other_station. To draw both stations as their own lines,
      use chart_over_time with station_2.
    - One point per grain; blank picks from the span. trend adds a trend line of the gap.
    - Summaries give the gap over the years both stations have: positive means station is higher.
    - No period: the whole record.
    - Returns the chart as a PNG: show it to the user. Describe it from summaries, never guessing
      at points. A CSV link, when present, is for the user to fetch; don't read it unless they ask
      for the data.
    e.g. metric TMAX, grain year, station Reagan's ghcn_id, other_station Dulles's ghcn_id.
    """
    return _chart(tools.chart_station_difference, locals())


@tool("Chart climograph")
def chart_climograph(
    station: str,
    precip: fields.RainOrSnow | None = None,
    normal: bool = False,
    period: fields.Period = None,
):
    """
    A climograph: monthly average highs and lows as lines with monthly rain or snow totals as bars:
    "plot a climograph for Dulles", "show the climate of Denver with snowfall".
    - precip blank: PRCP. Over the whole record, the period, or with normal on the 1991-2020
      normals (not both normal and a period). One station only.
    - Returns the chart as a PNG: show it to the user. Describe it from summaries, never guessing
      at points. A CSV link, when present, is for the user to fetch; don't read it unless they ask
      for the data.
    e.g. normal true.
    """
    return _chart(
        tools.chart_climograph,
        {
            "station": station,
            "precip": precip or "PRCP",
            "normal": normal,
            "period": period,
        },
    )


@tool("Chart a ranking")
def chart_ranking(
    station: str,
    metric: fields.Metric,
    ranking_of: fields.RankingOf,
    order: fields.Order,
    n: fields.N = None,
    measure: fields.Measure | None = None,
    season: fields.Season | None = None,
    months: list[int] | None = None,
    period: fields.Period = None,
):
    """
    A ranking as a bar chart: "plot the 10 biggest snowstorms", "chart the 5 hottest days", "graph
    the snowiest winters".
    - ranking_of: day, storm (metric PRCP or SNOW), or whole periods: month, year, season (with
      season set), or snow_season (SNOW only, July-June).
    - order highest or lowest; for storms, largest or smallest.
    - measure only for storms: duration ranks by length in days ("longest rainy spells").
    - months only for days and periods ("hottest January days": months [1]). No period: the whole record.
    - Returns the chart as a PNG: show it to the user. Describe it from the result's rows, never
      guessing at bars.
    e.g. metric SNOW, ranking_of storm, order highest, n 10.
    """
    args = {"station": station, "metric": metric, "ranked": True, "n": n, "period": period}
    if ranking_of != "storm" and measure:
        raise ToolError("measure is only for ranking_of storm: leave it blank")
    if ranking_of in ("day", "storm") and season:
        raise ToolError("season is only for ranking whole periods: use months for a day, or leave it blank")
    if ranking_of == "storm":
        if months:
            raise ToolError("months isn't available for ranking_of storm: leave it blank")
        select = "largest" if order == "highest" else "smallest"
        return _ranking_chart(tools.events, {**args, "select": select, "measure": measure})
    if ranking_of == "day":
        return _ranking_chart(tools.extreme_days, {**args, "order": order, "months": months})
    return _ranking_chart(
        tools.compare_periods,
        {**args, "period_type": ranking_of, "select": order, "season": season, "months": months},
    )
