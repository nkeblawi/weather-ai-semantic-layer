"""
Altair chart templates for /plot answers.

Each function takes a chart dict from series.group_charts (plain data, kept in
the session history) and returns an Altair chart; the app draws it with
st.altair_chart, which applies the Streamlit light/dark theme. The model never
writes chart code -- it only picks the query; these fixed templates do the
drawing.
"""

from __future__ import annotations

import altair as alt
import pandas as pd

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


def _legend(names: list[str]) -> alt.Legend | None:
    """Legend above the plot; one entry per row when the names won't fit across a phone."""
    if len(names) < 2:
        return None
    one_per_row = sum(len(n) for n in names) > 28
    return alt.Legend(title=None, orient="top", direction="vertical" if one_per_row else "horizontal")


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
    name_axis = (chart["kind"] == "climatology" and chart["unit"] != "day_of_year") or chart["unit"] == "month"
    return name_axis or len(chart["series"]) == 1


def _x(chart: dict, df: pd.DataFrame):
    """
    Dates for a time series; month/season names in order for a climatology.
    Monthly bars also use names: on a date axis Vega sizes bars for daily
    spacing, so monthly bars would be slivers.
    """
    if chart["unit"] == "day_of_year":
        # calendar days on a leap-year (2000) axis; only the month shows
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
    if _bars(chart):
        marks = base.mark_bar().encode(
            x=x, y=y, color=color, tooltip=_tooltip(chart, extra),
            **({"xOffset": "series:N"} if len(names) > 1 else {}),
            # daily bars span their day (start to next day); otherwise Vega
            # draws them as hairlines on a date axis
            # (with x2, Vega needs the y baseline too, or it draws a square)
            **({"x2": "x_end:T", "y2": alt.datum(0)} if chart["unit"] == "day" else {}),
        )
    else:
        many = len(df) > 120
        marks = base.mark_line(point=not many, strokeWidth=1.5 if many else 2).encode(
            x=x, y=y, color=color, tooltip=_tooltip(chart, extra)
        )
    layers = [marks]
    if chart["kind"] == "climatology" and "lower" in df and not _bars(chart):
        # +/- 1 standard deviation of the yearly values: the usual range
        layers.insert(0, base.mark_area(opacity=0.15).encode(
            x=x, y="lower:Q", y2="upper:Q", color=_band_color(names, colors),
            tooltip=_tooltip(chart, extra),
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
    if has_normal:
        # Over bars, the normal is neutral gray so it doesn't blend into them
        normal_color = alt.value(NORMAL_GRAY) if bars else _band_color(names, colors)
        band = base.mark_area(opacity=0.12 if bars else 0.15)
        normal = base.mark_line(strokeDash=[6, 4], strokeWidth=2 if bars else 1.5, point=bars)
        layers.append(band.encode(x=x, y=alt.Y("lower:Q", title=y_title), y2="upper:Q", color=normal_color, tooltip=tooltip))
        layers.append(normal.encode(x=x, y="normal:Q", color=normal_color, tooltip=tooltip))
    if bars:
        layers.insert(0, base.mark_bar(opacity=0.85).encode(
            x=x, y=alt.Y("value:Q", title=y_title), color=color, tooltip=tooltip,
            **({"xOffset": "series:N"} if len(names) > 1 else {}),
        ))
    else:
        layers.append(base.mark_line(point=len(df) <= 120, strokeWidth=2).encode(
            x=x, y=alt.Y("value:Q", title=y_title, scale=alt.Scale(zero=False)), color=color, tooltip=tooltip
        ))
    layers += _trend_layers(chart, "value")
    return alt.layer(*layers).resolve_legend(color="independent").properties(height=HEIGHT)


def anomaly_chart(chart: dict) -> alt.Chart:
    """Departure from normal: bars colored by sign around a zero line."""
    df = _frame(chart)
    df = df[df["anomaly"].notna()] if "anomaly" in df else df.iloc[0:0]
    x = _x(chart, df)
    bars = alt.Chart(df).mark_bar().encode(
        x=x,
        y=alt.Y("anomaly:Q", title=f"Departure ({chart['units']})"),
        color=alt.condition("datum.anomaly >= 0", alt.value(ABOVE), alt.value(BELOW)),
        tooltip=[
            alt.Tooltip("series:N", title="Series"),
            alt.Tooltip("label:N", title="When"),
            alt.Tooltip("value:Q", title=chart["units"] or "Value"),
            alt.Tooltip("normal:Q", title="Normal"),
            alt.Tooltip("anomaly:Q", title="Departure"),
        ],
        **({"xOffset": "series:N"} if len(chart["series"]) > 1 else {}),
    )
    zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(strokeWidth=1).encode(y="y:Q")
    return alt.layer(bars, zero, *_trend_layers(chart, "anomaly")).properties(height=HEIGHT)


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


def build_chart(chart: dict) -> alt.Chart:
    """The Altair chart for a chart dict from series.group_charts."""
    if chart["kind"] == "anomaly":
        built = anomaly_chart(chart)
    elif chart["kind"] == "overlay":
        built = normals_overlay_chart(chart)
    else:
        built = time_series_chart(chart)
    return built.properties(title=_title(chart))


def chart_spec(chart: dict) -> dict:
    """
    The Vega-Lite spec the app draws (st.vega_lite_chart), with the legend
    centered under the centered title. Vega supports legend layout, but
    Altair's schema rejects it, so it's added to the finished spec.
    """
    spec = build_chart(chart).to_dict()
    spec.setdefault("config", {}).setdefault("legend", {})["layout"] = {"top": {"anchor": "middle"}}
    return spec


def to_csv(chart: dict) -> str:
    """The chart's points as CSV, for the download button."""
    df = _frame(chart)
    cols = [c for c in ("series", "label", "value", "normal", "anomaly", "stddev") if c in df]
    return df[cols].rename(columns={"label": "when", "value": f"value ({chart['units']})"}).to_csv(index=False)
