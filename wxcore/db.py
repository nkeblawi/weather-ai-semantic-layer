"""
Databricks access: the service principal connection, lookup_station, and
answer_query, the Unity Catalog functions the front ends call.

Requires these environment variables, read when they're used (never at
import). wxcore doesn't load .env files: the caller sets the environment
(the app loads its .env; a hosted server gets them from the platform).
    WX_CATALOG          (weather for prod, weather_dev for dev; no default)
    DATABRICKS_SERVER
    DATABRICKS_HTTP     (SQL warehouse connection details page in Databricks)
    DATABRICKS_CLIENT   (service principal application/client ID)
    DATABRICKS_SECRET   (service principal OAuth secret)

NOTE: the exact parameter-binding syntax below (":name" style, passed via
the `parameters=` dict to cursor.execute) is databricks-sql-connector's
native-parameters feature.
"""

import json
import os
from datetime import date

from databricks import sql as databricks_sql
from databricks.sdk.core import Config, oauth_service_principal

from wxcore.dates import (
    SEASON_START_MONTH,
    SNOW_SEASON_START_MONTH,
    calendar_day_label,
    season_day,
    season_months,
    smooth_day_of_year,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MATCHING_DAYS_MAX = 5  # a day count this small also names the days


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------


def _service_principal_credentials():
    config = Config(
        host=f"https://{os.environ['DATABRICKS_SERVER']}",
        client_id=os.environ["DATABRICKS_CLIENT"],
        client_secret=os.environ["DATABRICKS_SECRET"],
    )
    return oauth_service_principal(config)


def _sql_bucketing(entry: dict) -> tuple[str, list | None, int | None]:
    """
    (p_unit, p_month_filter, p_year_start_month) for answer_query. The
    resolver's snow_season and season units are year buckets in SQL: a
    snow season starts in July; a season keeps only its months.
    """
    unit = entry.get("unit", "day")
    if unit == "snow_season":
        return "year", None, SNOW_SEASON_START_MONTH
    if unit == "season":
        season = entry["season"]
        return "year", season_months(season), SEASON_START_MONTH.get(season)
    return unit, entry.get("month_filter"), None


def _calendar_day_result(entry: dict, result: dict | None) -> dict | None:
    """
    One calendar day out of a day_of_year result, as a single-value result:
    the record (max/min, with the year set) or the smoothed normal (mean).
    """
    if not result or result.get("error") or entry["calendar_day"] not in result.get("days", []):
        return {"value": None}
    i = result["days"].index(entry["calendar_day"])
    agg = entry["aggregation"]
    out = {"calendar_day": calendar_day_label(entry["calendar_day"]), "n_years": result["n_years"][i]}
    if entry.get("period_aggregation") == "any":
        # odds: the share of years the condition held on that day, as a percent
        out["value"] = round(result["values"][i] * 100, 1)
    elif agg in ("max", "min"):
        out.update(value=result[agg][i], record_year=result[f"{agg}_year"][i])
    else:
        out["value"] = smooth_day_of_year(result["values"])[i]
    return out


def _date_result(entry: dict, result: dict | None) -> dict:
    """
    first_date / last_date answers: one year -> that date; several -> the
    earliest and latest (by time of year) with their years, the median date,
    and how many years had no such day.
    """
    field = "first" if entry["aggregation"] == "first_date" else "last"
    if not result or result.get("error"):
        return {"value": None}
    pairs = [(y, d) for y, d in zip(result.get("years", []), result.get(field, []))]
    found = [(y, d) for y, d in pairs if d]
    if len(pairs) == 1:
        return {"value": found[0][1] if found else None, "bucket_year": pairs[0][0]}
    if not found:
        return {"value": None, "years_counted": len(pairs)}
    snow = entry.get("unit") == "snow_season"
    by_day = sorted(found, key=lambda yd: season_day(yd[1], snow))
    mid = by_day[len(by_day) // 2][1]
    # every year that shares the earliest (or latest) day: a tie is reported, not broken
    tied = lambda end: [d for _, d in found if season_day(d, snow) == season_day(end, snow)]
    one_or_all = lambda ds: ds[0] if len(ds) == 1 else sorted(ds)
    years = [y for y, _ in pairs]
    return {"value": None, "typical_date": date.fromisoformat(mid).strftime("%B %-d"),
            "earliest_on_record": one_or_all(tied(by_day[0][1])), "latest_on_record": one_or_all(tied(by_day[-1][1])),
            "data_years": f"{min(years)}-{max(years)}", "years_counted": len(pairs), "years_without": len(pairs) - len(found)}


def _max_min_result(ranked: dict | None) -> dict:
    """
    max/min answers come from the top/bottom 1, so a tie is reported, not
    broken: the answer is the most recent rank-1 row (rows are in date order
    within a rank), and with a tie, `ties` lists every rank-1 row.
    """
    rows = (ranked or {}).get("rows") or []
    if not rows:
        return {"value": None}
    result = dict(rows[-1])
    if len(rows) > 1:
        result["ties"] = rows
    return result


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------


def catalog() -> str:
    """
    The Unity Catalog catalog holding the analytics functions and views:
    WX_CATALOG, read on every call. Raises when it's unset rather than
    falling back to prod.
    """
    name = os.environ.get("WX_CATALOG", "").strip()
    if not name:
        raise RuntimeError(
            "WX_CATALOG is not set: set it to weather (prod) or weather_dev (dev) "
            "in the environment, or in the app's .env"
        )
    return name


def get_databricks_connection():
    return databricks_sql.connect(
        server_hostname=os.environ["DATABRICKS_SERVER"],
        http_path=os.environ["DATABRICKS_HTTP"],
        credentials_provider=_service_principal_credentials,
    )


def call_lookup_station(conn, location_text: str):
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {catalog()}.analytics.lookup_station(:location_text)",
            parameters={"location_text": location_text},
        )
        row = cur.fetchone()
    if not row or row[0] is None:
        return None
    return json.loads(row[0])


def fill_station_info(conn, queries, station_info: dict) -> None:
    """
    Adds to station_info the stations these queries use without a
    lookup_station result, when the id is a real station. The skill's worked
    examples use real ids (Dulles above all), so a model sometimes reuses one
    from memory; the id is right, but validation only trusts station_info.
    An id that isn't in the stations table stays missing and is still
    rejected.
    """
    missing = {
        q.get("station_id")
        for q in queries or []
        if isinstance(q, dict) and isinstance(q.get("station_id"), str)
    } - set(station_info)
    if not missing:
        return
    params = {f"id{i}": sid for i, sid in enumerate(sorted(missing))}
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT ghcn_id, name, city, state FROM {catalog()}.analytics.stations "
            f"WHERE ghcn_id IN ({', '.join(':' + k for k in params)})",
            parameters=params,
        )
        rows = cur.fetchall()
    for ghcn_id, name, city, state in rows:
        station_info[ghcn_id] = {"ghcn_id": ghcn_id, "name": name, "city": city, "state": state}


def place_label(city: str, state: str | None) -> str:
    """
    A city as shown to users: "Portland, OR", "Montreal, QC". A city that
    already carries its own qualifier (e.g. "Washington, DC", stored with
    state VA) is used as-is, and so is one with no state.
    """
    return city if "," in city or not state else f"{city}, {state}"


def station_place(station: dict, station_id: str) -> str:
    """
    The place a station's answers, titles, and subtitles name: its city
    with state/province, else its station name, else the id. Station info
    saved before lookup_station returned state shows the bare city.
    """
    if station.get("city"):
        return place_label(station["city"], station.get("state"))
    return station.get("name") or station_id


def list_home_locations(conn) -> dict[str, str]:
    """
    {station code: display name} for the home-location picker, ordered by
    name, e.g. {"KIAD": "Sterling, VA (KIAD)"}. The code (ICAO, else IATA) is
    the value saved as home_location and given to the resolver -- an exact
    lookup_station key. The display name is presentation only (place_label).
    """
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT coalesce(icao, iata) AS code, city, state
            FROM {catalog()}.analytics.stations
            WHERE coalesce(icao, iata) IS NOT NULL AND city IS NOT NULL
            ORDER BY city, code
            """)
        rows = cur.fetchall()
    return {
        code: f"{place_label(city, state)} ({code})"
        for code, city, state in rows
    }


def call_answer_query(conn, entry: dict):
    # A record or normal for one calendar day ("record high on July 4") is a
    # day_of_year series in SQL, reduced to that day here
    if entry.get("unit") == "day_of_year" and entry["aggregation"] != "series":
        return _calendar_day_result(entry, call_answer_query(conn, {**entry, "aggregation": "series"}))
    # First/last date ("last spring freeze in 2025"): a dates series, reduced here
    if entry["aggregation"] in ("first_date", "last_date"):
        return _date_result(entry, call_answer_query(
            conn, {**entry, "aggregation": "series", "period_aggregation": entry["aggregation"]}))
    sql_unit, month_filter, year_start_month = _sql_bucketing(entry)
    # Max/min ("hottest day in 1980"): the top/bottom 1, reduced here
    if entry["aggregation"] in ("max", "min") and sql_unit in ("day", "event", "month", "year"):
        ranking = "top" if entry["aggregation"] == "max" else "bottom"
        return _max_min_result(call_answer_query(conn, {**entry, "aggregation": ranking, "limit": 1}))
    threshold = entry.get("threshold") or {}
    event_day_threshold = entry.get("event_day_threshold") or {}
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {catalog()}.analytics.answer_query(
                p_station_id => :station_id,
                p_metric => :metric,
                p_aggregation => :aggregation,
                p_start_date => :start_date,
                p_end_date => :end_date,
                p_unit => :unit,
                p_threshold_operator => :threshold_operator,
                p_threshold_value => :threshold_value,
                p_event_day_threshold_operator => :event_day_threshold_operator,
                p_event_day_threshold_value => :event_day_threshold_value,
                p_event_value => :event_value,
                p_period_aggregation => :period_aggregation,
                p_month_filter => from_json(:month_filter, 'ARRAY<INT>'),
                p_limit => :limit,
                p_year_start_month => :year_start_month
            )
            """,
            parameters={
                "station_id": entry["station_id"],
                "metric": entry["metric"],
                "aggregation": entry["aggregation"],
                "start_date": entry["start_date"],
                "end_date": entry["end_date"],
                "unit": sql_unit,
                "threshold_operator": threshold.get("operator"),
                "threshold_value": threshold.get("value"),
                "event_day_threshold_operator": event_day_threshold.get("operator"),
                "event_day_threshold_value": event_day_threshold.get("value"),
                "event_value": entry.get("event_value"),
                "period_aggregation": entry.get("period_aggregation"),
                # JSON text: the connector does not bind a Python list as a SQL ARRAY
                "month_filter": json.dumps(month_filter) if month_filter else None,
                "limit": entry.get("limit"),
                "year_start_month": year_start_month,
            },
        )
        row = cur.fetchone()
    if not row or row[0] is None:
        return None
    return json.loads(row[0])


def matching_days(conn, entry: dict, result: dict | None) -> list[dict] | None:
    """
    The days behind a small daily count ("has DCA ever hit 80 in January?"
    -> 1, on Jan 26, 2024), so the answer can name them. A count only
    returns the number; this ranks the days the threshold points at (top for
    >/>=, bottom for </<=) and keeps those that meet it. None when the count
    is 0, too large, or not a day count.
    """
    t = entry.get("threshold") or {}
    n = (result or {}).get("value")
    if (entry["aggregation"] != "count" or entry.get("unit", "day") != "day" or t.get("operator") not in (">", ">=", "<", "<=")
            or not isinstance(n, (int, float)) or not 1 <= n <= MATCHING_DAYS_MAX):
        return None
    ranked = {k: v for k, v in entry.items() if k != "threshold"} | {
        "aggregation": "top" if t["operator"] in (">", ">=") else "bottom", "limit": int(n)}
    rows = (call_answer_query(conn, ranked) or {}).get("rows") or []
    meets = {">": lambda v: v > t["value"], ">=": lambda v: v >= t["value"],
             "<": lambda v: v < t["value"], "<=": lambda v: v <= t["value"]}[t["operator"]]
    return [{"obs_date": r["obs_date"], "value": r["value"]} for r in rows if r.get("value") is not None and meets(r["value"])]
