"""
Altair chart templates for chart answers.

Each function takes a chart dict from series.group_charts (plain data) and
returns an Altair chart; chart_spec gives its Vega-Lite spec, which a front
end draws (the Streamlit app, with its light/dark theme) or renders to an
image. The model never writes chart code -- it only picks the query; these
fixed templates do the drawing.
"""

from __future__ import annotations

import json

import altair as alt
import pandas as pd


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Line colors by metric.
METRIC_COLORS = {
    "TMAX": "#E4572E",  # red: highs
    "TMIN": "#2E86DE",  # blue: lows
    "TAVG": "#F2A541",
    "PRCP": "#17A398",
    "SNOW": "#7FA7C9",
    "SNWD": "#7FA7C9",
    "AWND": "#8E7DBE",
    "HDD": "#2E86DE",
    "CDD": "#E4572E",
}
# Fallbacks for a second line of the same metric (another station): contrast
# first -- a blue next to a blue, or a red next to a red, is hard to tell apart
PALETTE = ["#F2A541", "#8E7DBE", "#17A398", "#E4572E", "#87CEEB"]
ABOVE, BELOW = "#E4572E", "#2E86DE"  # anomaly bars: warmer/wetter vs cooler/drier
NORMAL_GRAY = "#8C8C8C"
HEIGHT = 320


# Climate graph (NWS style): record range behind the normal range, daily bars on top
CLIMATE_PARTS = ["Record range", "Normal range", "Daily range", "Record high set", "Record low set"]
CLIMATE_COLORS = ["#B8C0CC", "#D9C8A9", "#4F6FA8", "#E4572E", "#2E86DE"]


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------


def _colors(chart: dict) -> tuple[list[str], list[str]]:
    names, colors, used = [], [], set()
    for i, s in enumerate(chart["series"]):
        color = METRIC_COLORS.get(s["metric"])
        if color is None or color in used:
            color = next(c for c in PALETTE if c not in used)
        used.add(color)
        names.append(s["name"])
        colors.append(color)
    return names, colors


def _toggled_legend_encode(field: str) -> dict:
    """
    Fade a legend entry while its item is hidden. Vega-Lite's default does the
    reverse (it fades every entry except the selected ones), which reads
    backwards when the selection is the hidden set.
    """
    hidden = f"length(data('hidden_store')) && hidden['{field}'] && indexof(hidden['{field}'], datum.value) >= 0"
    faded = [{"test": hidden, "value": 0.3}, {"value": 1}]
    # name + interactive are what Vega-Lite itself sets to make the legend
    # clickable; an encode block replaces its own, so they're repeated here
    part = lambda kind, update: {"name": f"{field}_legend_{kind}", "interactive": True, "update": update}
    return {
        "symbols": part("symbols", {"opacity": faded}),
        "labels": part("labels", {"opacity": faded}),
        "entries": part("entries", {"fill": {"value": "transparent"}}),
    }


def _legend(names: list[str]) -> alt.Legend | None:
    """Legend above the plot; one entry per row when the names won't fit across a phone."""
    if len(names) < 2:
        return None
    one_per_row = sum(len(n) for n in names) > 28
    return alt.Legend(title=None, orient="top", direction="vertical" if one_per_row else "horizontal",
                      symbolOpacity=1)


def _toggle(field: str):
    """
    Click a legend item to hide that layer or line, click again to show it.
    The selection holds the hidden items (empty = all shown); toggle keeps
    each click's item instead of replacing the last one. Named so the legend
    can read its store (_toggled_legend_encode).
    """
    return alt.selection_point(name="hidden", fields=[field], bind="legend", toggle="true", empty=False)


def _fade(hidden, opacity: float):
    """Opacity that drops to near zero while the item is toggled off."""
    return alt.condition(hidden, alt.value(0.04), alt.value(opacity))


def _band_color(names: list[str], colors: list[str]) -> alt.Color:
    """
    Same colors with no legend of its own, so the legend comes from the lines
    alone (a band's faint fill would otherwise show in the legend symbols).
    Needs resolve_legend(color="independent") on the layer.
    """
    return alt.Color("series:N", scale=alt.Scale(domain=names, range=colors), legend=None)


def _frame(chart: dict) -> pd.DataFrame:
    rows = []
    for s in chart["series"]:
        for p in s["points"]:
            rows.append({**p, "series": s["name"]})
    df = pd.DataFrame(rows)
    if chart["unit"] == "day" and not df.empty:
        df["x_end"] = (pd.to_datetime(df["x"]) + pd.Timedelta(days=1)).dt.strftime("%Y-%m-%d")
    return df


def _y_title(chart: dict) -> str:
    return chart["units"] or "value"


def _bars(chart: dict) -> bool:
    """
    Bars for totals and day counts -- except several series on a date axis
    (yearly, daily): Vega can only put bars side by side on a name axis
    (months, seasons), and on a date axis it stacks them, which reads as a
    sum. Those get lines instead.
    """
    if not chart["series"][0]["bars"]:
        return False
    name_axis = (chart["kind"] == "climatology" and chart["unit"] not in ("day_of_year", "week_of_year")) or chart["unit"] == "month"
    return name_axis or len(chart["series"]) == 1


def _x(chart: dict, df: pd.DataFrame):
    """
    Dates for a time series; month/season names in order for a climatology.
    Monthly bars also use names: on a date axis Vega sizes bars for daily
    spacing, so monthly bars would be slivers.
    """
    if chart["unit"] in ("day_of_year", "week_of_year"):
        # calendar days/weeks on a leap-year (2000) axis; only the month shows
        return alt.X("x:T", title=None, scale=alt.Scale(type="utc"), axis=alt.Axis(format="%b"))
    monthly_bars = chart["unit"] == "month" and _bars(chart)
    if chart["kind"] == "climatology" or monthly_bars:
        order = df.sort_values("x")["label"].drop_duplicates().tolist()
        return alt.X(
            "label:N", sort=order, title=None,
            axis=alt.Axis(labelAngle=0 if len(order) <= 12 else -45),
            scale=alt.Scale(paddingInner=0.25),  # gaps between bars
        )
    unit = chart["unit"]
    if unit == "day":
        return alt.X("x:T", title=None, scale=alt.Scale(type="utc"), axis=alt.Axis(format="%b %-d"))
    fmt = "%b %Y" if unit == "month" else "%Y"
    # A short span gets one tick per month (or year); left to Vega, it puts
    # weekly ticks that all read "Jun 2026". Long spans keep Vega's spacing.
    n = df["x"].nunique() if "x" in df else 0
    short = n <= (24 if unit == "month" else 15)
    ticks = {"interval": "month" if unit == "month" else "year", "step": 1} if short else None
    # Dates are UTC midnight; a local-time scale would show 2024-07-01 as
    # June 30 (and label it "Jun") anywhere west of Greenwich.
    return alt.X("x:T", title=None, scale=alt.Scale(type="utc"), axis=alt.Axis(format=fmt, **({"tickCount": ticks} if ticks else {})))


def _tooltip(chart: dict, extra: list[str] | None = None) -> list:
    return [
        alt.Tooltip("series:N", title="Series"),
        alt.Tooltip("label:N", title="When"),
        alt.Tooltip("value:Q", title=chart["units"] or "Value"),
        *[alt.Tooltip(f"{f}:Q", **({"format": "d"} if f.endswith("_year") else {})) for f in (extra or [])],
    ]


def _trend_layers(chart: dict, field: str) -> list:
    """A dashed least-squares line per series that asked for a trend."""
    rows = []
    for s in chart["series"]:
        t = s.get("trend")
        if t:
            label = f"{s['name']} trend ({t['per_decade']:+} {chart['units']}/decade)"
            rows += [
                {"x": t["start"]["x"], field: t["start"]["value"], "trend": label},
                {"x": t["end"]["x"], field: t["end"]["value"], "trend": label},
            ]
    if not rows:
        return []
    return [
        alt.Chart(pd.DataFrame(rows)).mark_line(strokeDash=[2, 3], strokeWidth=2, color="#888888").encode(
            x=alt.X("x:T", scale=alt.Scale(type="utc")), y=f"{field}:Q", detail="trend:N",
            tooltip=[alt.Tooltip("trend:N", title="Trend")],
        )
    ]


def _title(chart: dict) -> alt.TitleParams:
    """What is plotted, drawn inside the chart so it stays with a downloaded PNG."""
    text = chart.get("heading") or chart["title"]
    # Long headings wrap at their natural break so they fit a phone-width chart
    for sep in (" — ", ", "):
        if len(text) > 40 and sep in text:
            head, tail = text.split(sep, 1)
            text = [head, tail[0].upper() + tail[1:]]
            break
    return alt.TitleParams(
        text=text,
        subtitle=chart.get("subtitle") or [],
        anchor="middle", fontSize=15, subtitleFontSize=12, offset=8,
        # Streamlit's theme colors the title but not the subtitle; mid gray reads on light and dark
        subtitleColor=NORMAL_GRAY,
    )


def _encodings(spec: dict):
    """Every encoding block in a (possibly layered) spec."""
    if "encoding" in spec:
        yield spec["encoding"]
    for layer in spec.get("layer", []):
        yield from _encodings(layer)


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------


def time_series_chart(chart: dict) -> alt.Chart:
    """Values over time (or a climatology): lines, or bars for PRCP/SNOW totals."""
    df = _frame(chart)
    names, colors = _colors(chart)
    color = alt.Color("series:N", scale=alt.Scale(domain=names, range=colors),
                      legend=_legend(names))
    x = _x(chart, df)
    # totals and counts start at zero even when drawn as lines
    y = alt.Y("value:Q", title=_y_title(chart), scale=alt.Scale(zero=chart["series"][0]["bars"]))
    base = alt.Chart(df)
    extra = ["stddev"] if chart["kind"] == "climatology" else []
    if chart["unit"] == "day_of_year":
        extra += ["record_high", "record_high_year", "record_low", "record_low_year"]
    several = len(names) > 1
    hidden = _toggle("series") if several else None
    fade = (lambda o: {"opacity": _fade(hidden, o)}) if several else (lambda o: {"opacity": alt.value(o)})
    if _bars(chart):
        marks = base.mark_bar().encode(
            x=x, y=y, color=color, tooltip=_tooltip(chart, extra), **fade(1.0),
            **({"xOffset": "series:N"} if len(names) > 1 else {}),
            # daily bars span their day (start to next day); otherwise Vega
            # draws them as hairlines on a date axis
            # (with x2, Vega needs the y baseline too, or it draws a square)
            **({"x2": "x_end:T", "y2": alt.datum(0)} if chart["unit"] in ("day", "week_of_year") else {}),
        )
    else:
        many = len(df) > 120
        marks = base.mark_line(point=not many, strokeWidth=1.5 if many else 2).encode(
            x=x, y=y, color=color, tooltip=_tooltip(chart, extra), **fade(1.0)
        )
    layers = [marks.add_params(hidden) if several else marks]
    if chart["kind"] == "climatology" and "lower" in df and not _bars(chart):
        # +/- 1 standard deviation of the yearly values: the usual range
        layers.insert(0, base.mark_area().encode(
            x=x, y="lower:Q", y2="upper:Q", color=_band_color(names, colors),
            tooltip=_tooltip(chart, extra), **fade(0.15),
        ))
    layers += _trend_layers(chart, "value")
    return alt.layer(*layers).resolve_legend(color="independent").properties(height=HEIGHT)


def normals_overlay_chart(chart: dict) -> alt.Chart:
    """Target values vs. the normal (dashed) and its +/- 1 std dev band."""
    df = _frame(chart)
    names, colors = _colors(chart)
    color = alt.Color("series:N", scale=alt.Scale(domain=names, range=colors),
                      legend=_legend(names))
    x = _x(chart, df)
    y_title = _y_title(chart)
    base = alt.Chart(df)
    has_normal = "normal" in df and df["normal"].notna().any()
    bars = _bars(chart)
    layers = []
    # same tooltip on every layer: a layer without one gets Vega's default,
    # which shows raw fields
    tooltip = _tooltip(chart, ["normal", "anomaly"] if has_normal else [])
    several = len(names) > 1
    hidden = _toggle("series") if several else None
    fade = (lambda o: _fade(hidden, o)) if several else alt.value
    if has_normal:
        # Over bars, the normal is neutral gray so it doesn't blend into them
        normal_color = alt.value(NORMAL_GRAY) if bars else _band_color(names, colors)
        band = base.mark_area()
        normal = base.mark_line(strokeDash=[6, 4], strokeWidth=2 if bars else 1.5, point=bars)
        # a hidden series takes its normal and band with it
        layers.append(band.encode(x=x, y=alt.Y("lower:Q", title=y_title), y2="upper:Q", color=normal_color,
                                  tooltip=tooltip, opacity=fade(0.12 if bars else 0.15)))
        layers.append(normal.encode(x=x, y="normal:Q", color=normal_color, tooltip=tooltip, opacity=fade(1.0)))
    if bars:
        values = base.mark_bar().encode(
            x=x, y=alt.Y("value:Q", title=y_title), color=color, tooltip=tooltip, opacity=fade(0.85),
            **({"xOffset": "series:N"} if several else {}),
        )
        layers.insert(0, values.add_params(hidden) if several else values)
    else:
        values = base.mark_line(point=len(df) <= 120, strokeWidth=2).encode(
            x=x, y=alt.Y("value:Q", title=y_title, scale=alt.Scale(zero=False)), color=color, tooltip=tooltip,
            opacity=fade(1.0),
        )
        layers.append(values.add_params(hidden) if several else values)
    layers += _trend_layers(chart, "value")
    return alt.layer(*layers).resolve_legend(color="independent").properties(height=HEIGHT)


def anomaly_chart(chart: dict) -> alt.Chart:
    """
    Departure from normal around a zero line. One series: bars colored by
    sign. Several: colored by series (with a clickable legend) -- side by
    side on a month axis, lines on a date axis, where Vega would stack bars.
    """
    df = _frame(chart)
    df = df[df["anomaly"].notna()] if "anomaly" in df else df.iloc[0:0]
    x = _x(chart, df)
    tooltip = [
        alt.Tooltip("series:N", title="Series"),
        alt.Tooltip("label:N", title="When"),
        alt.Tooltip("value:Q", title=chart["units"] or "Value"),
        alt.Tooltip("normal:Q", title="Normal"),
        alt.Tooltip("anomaly:Q", title="Departure"),
    ]
    y = alt.Y("anomaly:Q", title=f"Departure ({chart['units']})")
    if len(chart["series"]) == 1:
        marks = alt.Chart(df).mark_bar().encode(
            x=x, y=y, color=alt.condition("datum.anomaly >= 0", alt.value(ABOVE), alt.value(BELOW)), tooltip=tooltip)
    else:
        names, colors = _colors(chart)
        hidden = _toggle("series")
        color = alt.Color("series:N", scale=alt.Scale(domain=names, range=colors), legend=_legend(names))
        name_axis = chart["unit"] == "month"
        mark = alt.Chart(df).mark_bar() if name_axis else alt.Chart(df).mark_line(point=len(df) <= 240, strokeWidth=2)
        marks = mark.encode(x=x, y=y, color=color, tooltip=tooltip, opacity=_fade(hidden, 1.0),
                            **({"xOffset": "series:N"} if name_axis else {})).add_params(hidden)
    zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(strokeWidth=1).encode(y="y:Q")
    return alt.layer(marks, zero, *_trend_layers(chart, "anomaly")).properties(height=HEIGHT)


def dates_chart(chart: dict) -> alt.Chart:
    """First/last dates by year (freeze dates): the y axis is the time of year on a reference year."""
    df = _frame(chart)
    names, colors = _colors(chart)
    several = len(names) > 1
    hidden = _toggle("series") if several else None
    color = alt.Color("series:N", scale=alt.Scale(domain=names, range=colors), legend=_legend(names))
    x = _x(chart, df)
    y = alt.Y("y_ref:T", title=None, scale=alt.Scale(type="utc"), axis=alt.Axis(format="%b %-d"))
    lines = alt.Chart(df).mark_line(point=True, strokeWidth=1.5).encode(
        x=x, y=y, color=color, opacity=_fade(hidden, 1.0) if several else alt.value(1.0),
        tooltip=[alt.Tooltip("series:N", title="Series"), alt.Tooltip("when:N", title="Date")])
    layers = [lines.add_params(hidden) if several else lines]
    rows = [{"x": s["trend"][e]["x"], "y_ref": s["trend"][e]["y_ref"],
             "trend": f"{s['name']} trend ({s['trend']['per_decade']:+} days/decade)"}
            for s in chart["series"] if s.get("trend") for e in ("start", "end")]
    if rows:
        layers.append(alt.Chart(pd.DataFrame(rows)).mark_line(strokeDash=[2, 3], strokeWidth=2, color="#888888").encode(
            x=alt.X("x:T", scale=alt.Scale(type="utc")), y=alt.Y("y_ref:T", scale=alt.Scale(type="utc")), detail="trend:N",
            tooltip=[alt.Tooltip("trend:N", title="Trend")]))
    return alt.layer(*layers).properties(height=HEIGHT)


def ranking_bar_chart(chart: dict) -> alt.Chart:
    """A ranking (a chart of top/bottom) as horizontal bars, rank 1 on top, values at the bar ends."""
    df = _frame(chart)
    metric = chart["series"][0]["metric"]
    color = METRIC_COLORS.get(metric, PALETTE[0])
    totals = metric in ("PRCP", "SNOW", "SNWD") or chart["units"] in ("days",)
    # every label shown: Vega otherwise drops alternate ones when the font is larger
    y = alt.Y("label:N", sort=alt.SortField("x"), title=None, axis=alt.Axis(labelOverlap=False, labelLimit=200))
    x = alt.X("value:Q", title=chart["units"], scale=alt.Scale(zero=totals))
    bars = alt.Chart(df).mark_bar(color=color).encode(
        x=x, y=y, tooltip=[alt.Tooltip("rank:Q", title="Rank"), alt.Tooltip("label:N", title="When"),
                           alt.Tooltip("value:Q", title=chart["units"])])
    # mid gray reads on light and dark themes
    text = alt.Chart(df).mark_text(align="left", dx=4, color=NORMAL_GRAY).encode(x=x, y=y, text=alt.Text("value:Q"))
    return alt.layer(bars, text).properties(height=max(120, 34 * len(df)))


def difference_chart(chart: dict) -> alt.Chart:
    """One station minus another, around a zero line."""
    df = _frame(chart)
    x = _x(chart, df)
    line = alt.Chart(df).mark_line(point=len(df) <= 120, strokeWidth=2, color=PALETTE[1]).encode(
        x=x, y=alt.Y("value:Q", title=f"Difference ({chart['units']})"),
        tooltip=[alt.Tooltip("label:N", title="When"), alt.Tooltip("value:Q", title=f"Difference ({chart['units']})")])
    zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(strokeWidth=1).encode(y="y:Q")
    return alt.layer(zero, line).properties(height=HEIGHT)


def climograph_chart(chart: dict) -> alt.Chart:
    """Monthly precipitation as bars (right axis) under temperature lines (left axis)."""
    df = _frame(chart)
    names, colors = _colors(chart)
    hidden = _toggle("series")
    color = alt.Color("series:N", scale=alt.Scale(domain=names, range=colors), legend=_legend(names))
    order = df.sort_values("x")["label"].drop_duplicates().tolist()
    x = alt.X("label:N", sort=order, title=None, axis=alt.Axis(labelAngle=0))
    temps = [s["name"] for s in chart["series"] if s["metric"] in ("TMAX", "TMIN", "TAVG")]
    temp_units = next(s["units"] for s in chart["series"] if s["name"] in temps)
    wet = [s for s in chart["series"] if s["name"] not in temps]
    base = alt.Chart(df)
    bars = base.transform_filter(alt.FieldOneOfPredicate("series", [s["name"] for s in wet])).mark_bar(opacity=0.6).encode(
        x=x, y=alt.Y("value:Q", title=f"{wet[0]['metric_label']} ({wet[0]['units']})", axis=alt.Axis(orient="right")),
        color=color, opacity=_fade(hidden, 0.6), tooltip=_tooltip(chart))
    lines = base.transform_filter(alt.FieldOneOfPredicate("series", temps)).mark_line(point=True, strokeWidth=2).encode(
        x=x, y=alt.Y("value:Q", title=f"Temperature ({temp_units})", scale=alt.Scale(zero=False)),
        color=color, opacity=_fade(hidden, 1.0), tooltip=_tooltip(chart))
    return alt.layer(bars.add_params(hidden), lines).resolve_scale(y="independent").properties(height=HEIGHT)


def histogram_chart(chart: dict) -> alt.Chart:
    """
    Counts per bin as touching bars, with the median marked. Several series
    (two stations) overlap, semi-transparent, with a clickable legend.
    """
    df = _frame(chart)
    names, colors = _colors(chart)
    several = len(names) > 1
    hidden = _toggle("series") if several else None
    color = alt.Color("series:N", scale=alt.Scale(domain=names, range=colors), legend=_legend(names))
    count_title = "years" if chart["unit"] != "value" else "days"
    bars = alt.Chart(df).mark_bar(binSpacing=0, stroke="white", strokeWidth=0.3).encode(
        x=alt.X("x:Q", title=chart["units"] if chart["units"].startswith("°") else chart["units"].capitalize(),
                scale=alt.Scale(zero=False)),
        x2="x_end:Q", y=alt.Y("value:Q", title=f"Number of {count_title}", stack=None), y2=alt.datum(0), color=color,
        opacity=_fade(hidden, 0.6) if several else alt.value(0.9),
        tooltip=[alt.Tooltip("series:N", title="Series"), alt.Tooltip("label:N", title="Range"),
                 alt.Tooltip("value:Q", title=count_title.capitalize()), alt.Tooltip("pct:Q", title="% of all")])
    layers = [bars.add_params(hidden) if several else bars]
    medians = pd.DataFrame([{"series": s["name"], "median": s["hist_stats"]["median"]} for s in chart["series"]
                            if (s.get("hist_stats") or {}).get("median") is not None])
    if not medians.empty:
        layers.append(alt.Chart(medians).mark_rule(strokeDash=[4, 3], strokeWidth=2).encode(
            x="median:Q", color=color, tooltip=[alt.Tooltip("series:N", title="Series"), alt.Tooltip("median:Q", title="Median")]))
    return alt.layer(*layers).properties(height=HEIGHT)


def climate_chart(chart: dict) -> alt.Chart:
    """
    Daily highs and lows as bars (low to high) over the normal range (normal
    low to normal high) and the record range (record low to record high).
    With one metric, a line over the same backdrop. Records set (or tied)
    in the period are marked.
    """
    by = {s["metric"]: s for s in chart["series"]}
    hi_s, lo_s = by.get("TMAX"), by.get("TMIN")
    rows: dict[str, dict] = {}
    for s in chart["series"]:
        for p in s["points"]:
            r = rows.setdefault(p["x"], {"x": p["x"], "label": p["label"]})
            key = "hi" if s is hi_s else "lo" if s is lo_s else "v"
            r[key], r[f"n_{key}"] = p["value"], p.get("normal")
            r[f"{key}_lower"], r[f"{key}_upper"] = p.get("lower"), p.get("upper")
            r[f"r_hi_{key}"], r[f"r_hi_{key}_year"] = p.get("record_high"), p.get("record_high_year")
            r[f"r_lo_{key}"], r[f"r_lo_{key}_year"] = p.get("record_low"), p.get("record_low_year")
            if p.get("record_set"):
                r["record_set"] = (r.get("record_set", "") + " " + p["record_set"]).strip()
                r["record_value"] = p["value"]
    df = pd.DataFrame(sorted(rows.values(), key=lambda r: r["x"]))
    df["x_end"] = (pd.to_datetime(df["x"]) + pd.Timedelta(days=1)).dt.strftime("%Y-%m-%d")
    pair = hi_s is not None and lo_s is not None
    if pair:
        df["rec_top"], df["rec_bot"] = df["r_hi_hi"], df["r_lo_lo"]
        df["norm_top"], df["norm_bot"] = df["n_hi"], df["n_lo"]
    else:
        k = "hi" if hi_s is not None else "lo" if lo_s is not None else "v"
        df["rec_top"], df["rec_bot"] = df[f"r_hi_{k}"], df[f"r_lo_{k}"]
        df["norm_top"], df["norm_bot"] = df[f"{k}_upper"], df[f"{k}_lower"]
        df["line"] = df[k]
    color = alt.Color("part:N", scale=alt.Scale(domain=CLIMATE_PARTS, range=CLIMATE_COLORS),
                      legend=alt.Legend(title=None, orient="top", symbolOpacity=1, columns=3))
    hidden = _toggle("part")
    x = _x(chart, df)
    y_title = _y_title(chart)
    base = alt.Chart(df)
    when = alt.Tooltip("label:N", title="Day")
    layers = [
        base.transform_calculate(part="'Record range'").mark_area().encode(
            x=x, y=alt.Y("rec_bot:Q", title=y_title, scale=alt.Scale(zero=False)), y2="rec_top:Q", color=color,
            opacity=_fade(hidden, 0.3),
            tooltip=[when, alt.Tooltip("rec_top:Q", title="Record high"), alt.Tooltip("rec_bot:Q", title="Record low")]),
        base.transform_calculate(part="'Normal range'").mark_area().encode(
            x=x, y="norm_bot:Q", y2="norm_top:Q", color=color, opacity=_fade(hidden, 0.85),
            tooltip=[when, alt.Tooltip("norm_top:Q", title="Normal high" if pair else "Normal + 1 sd"),
                     alt.Tooltip("norm_bot:Q", title="Normal low" if pair else "Normal - 1 sd")]),
    ]
    if pair:
        layers.append(base.transform_calculate(part="'Daily range'").mark_bar().encode(
            x=x, x2="x_end:T", y="lo:Q", y2="hi:Q", color=color, opacity=_fade(hidden, 1.0),
            tooltip=[when, alt.Tooltip("hi:Q", title="High"), alt.Tooltip("lo:Q", title="Low"),
                     alt.Tooltip("n_hi:Q", title="Normal high"), alt.Tooltip("n_lo:Q", title="Normal low"),
                     alt.Tooltip("r_hi_hi:Q", title="Record high"), alt.Tooltip("r_hi_hi_year:Q", title="Set", format="d"),
                     alt.Tooltip("r_lo_lo:Q", title="Record low"), alt.Tooltip("r_lo_lo_year:Q", title="Set", format="d")]))
    else:
        layers.append(base.transform_calculate(part="'Daily range'").mark_line(strokeWidth=1.5).encode(
            x=x, y="line:Q", color=color, opacity=_fade(hidden, 1.0),
            tooltip=[when, alt.Tooltip("line:Q", title=chart["units"])]))
    if "record_set" in df and df["record_set"].notna().any():
        layers.append(base.transform_filter("datum.record_set").transform_calculate(
            part="indexof(datum.record_set, 'low') >= 0 ? 'Record low set' : 'Record high set'")
                      .mark_point(filled=True, size=45, stroke="white", strokeWidth=0.8).encode(
            x=x, y="record_value:Q", color=color, opacity=_fade(hidden, 1.0),
            tooltip=[when, alt.Tooltip("record_set:N", title="Record"), alt.Tooltip("record_value:Q", title=chart["units"])]))
    return alt.layer(*layers).add_params(hidden).properties(height=HEIGHT)


def cumulative_chart(chart: dict) -> alt.Chart:
    """
    Running totals; several seasons share an axis aligned by day of season.
    The normal accumulation (first series') is a dashed gray line.
    """
    df = _frame(chart)
    names, colors = _colors(chart)
    several = len(chart["series"]) > 1
    xf = "x_aligned" if several else "x"
    span = (pd.to_datetime(df[xf]).max() - pd.to_datetime(df[xf]).min()).days if not df.empty else 0
    monthly = several or span > 60  # past ~2 months, a tick per month
    x = alt.X(f"{xf}:T", title=None, scale=alt.Scale(type="utc"),
              axis=alt.Axis(format="%b" if monthly else "%b %-d", **({"tickCount": {"interval": "month", "step": 1}} if monthly else {})))
    color = alt.Color("series:N", scale=alt.Scale(domain=names, range=colors), legend=_legend(names))
    tooltip = [alt.Tooltip("series:N", title="Series"), alt.Tooltip("label:N", title="Through"),
               alt.Tooltip("value:Q", title=f"Total ({chart['units']})"), alt.Tooltip("daily:Q", title="That day")]
    layers = []
    first = chart["series"][0]["name"]
    if "normal" in df and df["normal"].notna().any():
        normal = df[df["series"] == first]
        layers.append(alt.Chart(normal).mark_line(strokeDash=[6, 4], strokeWidth=2, color=NORMAL_GRAY).encode(
            x=x, y="normal:Q", tooltip=[alt.Tooltip("label:N", title="Through"), alt.Tooltip("normal:Q", title="Normal to date")]))
    hidden = _toggle("series") if several else None
    totals = alt.Chart(df).mark_line(strokeWidth=2, interpolate="step-after").encode(
        x=x, y=alt.Y("value:Q", title=f"Total ({chart['units']})"), color=color, tooltip=tooltip,
        opacity=_fade(hidden, 1.0) if several else alt.value(1.0))
    layers.append(totals.add_params(hidden) if several else totals)
    return alt.layer(*layers).properties(height=HEIGHT)


def build_chart(chart: dict) -> alt.Chart:
    """The Altair chart for a chart dict from series.group_charts."""
    if chart["kind"] == "dates":
        built = dates_chart(chart)
    elif chart["kind"] == "ranking":
        built = ranking_bar_chart(chart)
    elif chart["kind"] == "difference":
        built = difference_chart(chart)
    elif chart["kind"] == "climograph":
        built = climograph_chart(chart)
    elif chart["kind"] == "histogram":
        built = histogram_chart(chart)
    elif chart["kind"] == "climate":
        built = climate_chart(chart)
    elif chart["kind"] == "cumulative":
        built = cumulative_chart(chart)
    elif chart["kind"] == "anomaly":
        built = anomaly_chart(chart)
    elif chart["kind"] == "overlay":
        built = normals_overlay_chart(chart)
    else:
        built = time_series_chart(chart)
    return built.properties(title=_title(chart))


def chart_spec(chart: dict) -> dict:
    """
    The chart's Vega-Lite spec, as front ends draw it, with the legend
    centered under the centered title. Vega supports legend layout, but
    Altair's schema rejects it, so it's added to the finished spec.
    """
    spec = build_chart(chart).to_dict()
    spec.setdefault("config", {}).setdefault("legend", {})["layout"] = {"top": {"anchor": "middle"}}
    # legend fading for hidden items: Altair's schema rejects legend encode too
    if '"name": "hidden"' in json.dumps(spec.get("params", [])) + json.dumps(spec.get("layer", [])):
        for enc in _encodings(spec):
            color = enc.get("color")
            if isinstance(color, dict) and isinstance(color.get("legend"), dict) and "field" in color:
                color["legend"]["encode"] = _toggled_legend_encode(color["field"])
    return spec


def to_csv(chart: dict) -> str:
    """The chart's points as CSV, for the download button."""
    df = _frame(chart)
    cols = [c for c in ("series", "label", "value", "normal", "anomaly", "stddev") if c in df]
    return df[cols].rename(columns={"label": "when", "value": f"value ({chart['units']})"}).to_csv(index=False)
