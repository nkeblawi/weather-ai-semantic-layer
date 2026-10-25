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
import logging
import math
import os
import re
import unicodedata
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

# A place lookup_station's names don't match, from its coordinates: a station
# whose city is that place (e.g. "New York" -> New York, NY) and within
# NEARBY_MILES is used; any other station up to NEARBY_MILES is offered to
# the user; up to FAR_MILES it's offered with a warning; beyond, none.
NEARBY_MILES = 50
FAR_MILES = 150
MAX_NEARBY = 3
EARTH_RADIUS_MILES = 3958.8

# station-name abbreviations, spelled out for the user ("WASHINGTON DULLES INTL AP")
NAME_WORDS = {
    "INTL": "International",
    "NATL": "National",
    "AP": "Airport",
    "A": "Airport",
}

# Where the stations are, in a no-station reply: countries ("the US and
# Canada"), or continents once there are more than MAX_COUNTRIES_NAMED.
# Countries not listed here use the stations table's country_name.
MAX_COUNTRIES_NAMED = 5
COUNTRY_NAMES = {"US": "the US", "GB": "the UK"}
CONTINENTS = {
    "US": "North America",
    "CA": "North America",
    "MX": "North America",
    "PR": "North America",
}

# A place's coordinates come from analytics.places (GeoNames cities with
# 1,000+ people). Qualifiers after the name ("Vienna, VA", "Paris, France")
# match a state/province or country by code or name; these are the other
# ways people write them. Canadian provinces have numeric GeoNames codes.
COUNTRY_ALIASES = {
    "usa": "us",
    "united states of america": "us",
    "america": "us",
    "uk": "gb",
    "england": "gb",
    "scotland": "gb",
    "wales": "gb",
    "britain": "gb",
    "great britain": "gb",
}
PROVINCE_NAMES = {
    "ab": "alberta",
    "bc": "british columbia",
    "mb": "manitoba",
    "nb": "new brunswick",
    "nl": "newfoundland and labrador",
    "ns": "nova scotia",
    "nt": "northwest territories",
    "nu": "nunavut",
    "on": "ontario",
    "pe": "prince edward island",
    "qc": "quebec",
    "sk": "saskatchewan",
    "yt": "yukon",
}
# a qualifier with no comma before it is at most this many words ("Newfoundland and Labrador")
MAX_QUALIFIER_WORDS = 3

logger = logging.getLogger(__name__)


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


def _miles(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def _station_label(station: dict) -> str:
    """'Washington Dulles International Airport (IAD)': a station as a user knows it."""
    words = [NAME_WORDS.get(w, w.capitalize()) for w in station["name"].split()]
    name = re.sub(r"\bAirport Airport\b", "Airport", " ".join(words))
    return f"{name} ({station['iata']})" if station.get("iata") else name


def _nearby(conn, latitude: float, longitude: float) -> list[dict]:
    """Active stations within FAR_MILES of a point, nearest first, with their distance."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT ghcn_id, name, city, state, iata, latitude, longitude FROM {catalog()}.analytics.stations"
        )
        rows = cur.fetchall()
    stations = []
    for ghcn_id, name, city, state, iata, lat, lon in rows:
        if lat is None or lon is None:
            continue
        miles = round(_miles(latitude, longitude, lat, lon))
        if miles <= FAR_MILES:
            stations.append(
                {
                    "ghcn_id": ghcn_id,
                    "name": name,
                    "city": city,
                    "state": state,
                    "iata": iata,
                    "miles": miles,
                }
            )
    return sorted(stations, key=lambda s: s["miles"])[:MAX_NEARBY]


def _ascii_key(text: str) -> str:
    """'Montréal ' -> 'montreal': accents dropped, lowercased, spaces collapsed (as places.name_key)."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return " ".join(text.lower().split())


def _place_label(place: dict) -> str:
    """'Vienna, VA' in the US, else 'Paris, France': a place as the user would write it."""
    name = place["place_name"]
    if "," in name:
        return name  # already qualified ("Washington, D.C.")
    if place["country_code"] == "US" and place.get("admin1_code"):
        return f"{name}, {place['admin1_code']}"
    return f"{name}, {place['country_name']}" if place.get("country_name") else name


def _query_place(conn, name: str, qualifiers: list[str]) -> dict | None:
    """
    The place analytics.places has for a name: an exact name before a
    longer one starting with it ("New York" -> New York City, "Washington"
    -> Washington, D.C.), then the most populous, narrowed by qualifiers
    (state/province or country, by code or name). Raises if the table
    isn't there.
    """
    params = {"name": name, "prefix": f"{name} %", "comma": f"{name},%"}
    where = []
    for i, q in enumerate(qualifiers):
        params[f"q{i}"] = q
        params[f"c{i}"] = COUNTRY_ALIASES.get(q, q)
        params[f"p{i}"] = PROVINCE_NAMES.get(q, q)
        where.append(
            f"(lower(admin1_code) = :q{i} OR lower(admin1_name) IN (:q{i}, :p{i}) "
            f"OR lower(country_code) = :c{i} OR lower(country_name) = :q{i})"
        )
    sql = (
        "SELECT place_name, country_code, country_name, admin1_code, admin1_name, latitude, longitude "
        f"FROM {catalog()}.analytics.places WHERE (name_key = :name OR name_key LIKE :prefix OR name_key LIKE :comma) "
        + "".join(f"AND {w} " for w in where)
        + "ORDER BY name_key = :name DESC, population DESC LIMIT 1"
    )
    with conn.cursor() as cur:
        cur.execute(sql, parameters=params)
        row = cur.fetchone()
    if not row:
        return None
    keys = (
        "place_name",
        "country_code",
        "country_name",
        "admin1_code",
        "admin1_name",
        "latitude",
        "longitude",
    )
    return dict(zip(keys, row))


def _find_place(conn, location_text: str) -> dict | None:
    """
    The place a name means (_query_place), with qualifiers after commas
    ("Vienna, VA"). Without commas, the whole text is tried as a name first,
    then its last one to MAX_QUALIFIER_WORDS words as the qualifier ("Vienna
    VA", "Paris Texas", "Vienna West Virginia"). None when nothing matches,
    or the table isn't there yet.
    """
    parts = [re.sub(r"[%_]", "", _ascii_key(part)) for part in location_text.split(",")]
    name, qualifiers = parts[0], [q for q in parts[1:] if q]
    if not name:
        return None
    tries = [(name, qualifiers)]
    words = name.split()
    if len(parts) == 1:
        tries += [
            (" ".join(words[:-k]), [" ".join(words[-k:])])
            for k in range(1, min(MAX_QUALIFIER_WORDS, len(words) - 1) + 1)
        ]
    try:
        for place_name, quals in tries:
            if place := _query_place(conn, place_name, quals):
                return place
    except Exception:  # analytics.places not loaded yet: fall back to the caller's coordinates
        logger.warning("place lookup failed for %r", location_text, exc_info=True)
    return None


def _same_place(place: str, station: dict) -> bool:
    """'New York' or 'new york, ny' names the station's city 'New York, NY'."""

    def city(text):
        return re.sub(r"[^a-z ]", "", text.split(",")[0].lower()).strip()

    return city(place) == city(station["city"])


def _and_list(names: list[str]) -> str:
    """'A', 'A and B', 'A, B, and C'."""
    return (
        " and ".join(names)
        if len(names) <= 2
        else ", ".join(names[:-1]) + f", and {names[-1]}"
    )


def _covered_places(conn) -> str:
    """'the US and Canada', or continents when there are many countries: where the active stations are."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT DISTINCT country_code, country_name FROM {catalog()}.analytics.stations"
        )
        rows = sorted(cur.fetchall(), key=lambda r: (r[0] != "US", r[0] or ""))
    if len(rows) > MAX_COUNTRIES_NAMED:
        continents = list(
            dict.fromkeys(CONTINENTS.get(code, "other regions") for code, _ in rows)
        )
        return _and_list(continents)
    return _and_list(
        [COUNTRY_NAMES.get(code) or (name or code).title() for code, name in rows]
    )


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
    if (
        not result
        or result.get("error")
        or entry["calendar_day"] not in result.get("days", [])
    ):
        return {"value": None}
    i = result["days"].index(entry["calendar_day"])
    agg = entry["aggregation"]
    out = {
        "calendar_day": calendar_day_label(entry["calendar_day"]),
        "n_years": result["n_years"][i],
    }
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
    tied = lambda end: [
        d for _, d in found if season_day(d, snow) == season_day(end, snow)
    ]
    one_or_all = lambda ds: ds[0] if len(ds) == 1 else sorted(ds)
    years = [y for y, _ in pairs]
    return {
        "value": None,
        "typical_date": date.fromisoformat(mid).strftime("%B %-d"),
        "earliest_on_record": one_or_all(tied(by_day[0][1])),
        "latest_on_record": one_or_all(tied(by_day[-1][1])),
        "data_years": f"{min(years)}-{max(years)}",
        "years_counted": len(pairs),
        "years_without": len(pairs) - len(found),
    }


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
    # which periods the answer covers (query_by_period only)
    for key in ("n_buckets", "first_bucket", "last_bucket"):
        if key in ranked:
            result[key] = ranked[key]
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


def call_lookup_station(conn, location_text: str, latitude=None, longitude=None):
    """
    The active station a place names: {ghcn_id, name, city, state} for a
    name, code, or alias match. Otherwise the place's coordinates (from
    analytics.places, else the caller's) give: the station in that city
    (adding nearest_to and miles), or {"match": "none", "nearby": [...],
    "ask_user": "..."}: stations within FAR_MILES and a question to put to
    the user before using one (with nothing in range, ask_user names the
    countries covered; with no place found, it asks where the place is).
    matched_place says which place the name was taken to mean.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {catalog()}.analytics.lookup_station(:location_text)",
            parameters={"location_text": location_text},
        )
        row = cur.fetchone()
    if row and row[0] is not None:
        return json.loads(row[0])
    # A place the table knows uses its coordinates, not the caller's: the
    # model's own coordinates can be wrong (a flipped longitude sent
    # "Alexandria, VA" across the world). Its coordinates are only for a
    # place too small for the table.
    place_name, matched = location_text, {}
    place = _find_place(conn, location_text)
    if place is not None:
        latitude, longitude = place["latitude"], place["longitude"]
        place_name = _place_label(place)
        matched = {"matched_place": place_name}
    elif latitude is None or longitude is None:
        ask = f"I couldn't find a place called {location_text}. Which city or town is it in or near?"
        return {"match": "none", "nearby": [], "ask_user": ask}
    nearby = _nearby(conn, latitude, longitude)
    same = [
        s
        for s in nearby
        if s["miles"] <= NEARBY_MILES and _same_place(location_text, s)
    ]
    if same:
        station = {k: same[0][k] for k in ("ghcn_id", "name", "city", "state")}
        return {
            **station,
            "nearest_to": location_text,
            "miles": same[0]["miles"],
            **matched,
        }
    if not nearby:
        ask = f"I don't have weather observation history near {place_name}. I have stations in {_covered_places(conn)}."
        return {"match": "none", "nearby": [], "ask_user": ask, **matched}
    first = nearby[0]
    ask = f"There's no station in {place_name}. The nearest is {_station_label(first)}, {first['miles']} miles away"
    if first["miles"] > NEARBY_MILES:
        ask += ", so its weather may differ"
    others = [
        f"{_station_label(s)}, {s['miles']} miles away"
        for s in nearby[1:]
        if s["miles"] <= NEARBY_MILES
    ]
    ask += f". Use it{', or ' + ' or '.join(others) if others else ''}?"
    return {"match": "none", "nearby": nearby, "ask_user": ask, **matched}


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
        station_info[ghcn_id] = {
            "ghcn_id": ghcn_id,
            "name": name,
            "city": city,
            "state": state,
        }


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
    return {code: f"{place_label(city, state)} ({code})" for code, city, state in rows}


def call_answer_query(conn, entry: dict):
    # A record or normal for one calendar day ("record high on July 4") is a
    # day_of_year series in SQL, reduced to that day here
    if entry.get("unit") == "day_of_year" and entry["aggregation"] != "series":
        return _calendar_day_result(
            entry, call_answer_query(conn, {**entry, "aggregation": "series"})
        )
    # First/last date ("last spring freeze in 2025"): a dates series, reduced here
    if entry["aggregation"] in ("first_date", "last_date"):
        return _date_result(
            entry,
            call_answer_query(
                conn,
                {
                    **entry,
                    "aggregation": "series",
                    "period_aggregation": entry["aggregation"],
                },
            ),
        )
    sql_unit, month_filter, year_start_month = _sql_bucketing(entry)
    # Max/min ("hottest day in 1980"): the top/bottom 1, reduced here
    if entry["aggregation"] in ("max", "min") and sql_unit in (
        "day",
        "event",
        "month",
        "year",
    ):
        ranking = "top" if entry["aggregation"] == "max" else "bottom"
        return _max_min_result(
            call_answer_query(conn, {**entry, "aggregation": ranking, "limit": 1})
        )
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
    if (
        entry["aggregation"] != "count"
        or entry.get("unit", "day") != "day"
        or t.get("operator") not in (">", ">=", "<", "<=")
        or not isinstance(n, (int, float))
        or not 1 <= n <= MATCHING_DAYS_MAX
    ):
        return None
    ranked = {k: v for k, v in entry.items() if k != "threshold"} | {
        "aggregation": "top" if t["operator"] in (">", ">=") else "bottom",
        "limit": int(n),
    }
    rows = (call_answer_query(conn, ranked) or {}).get("rows") or []
    meets = {
        ">": lambda v: v > t["value"],
        ">=": lambda v: v >= t["value"],
        "<": lambda v: v < t["value"],
        "<=": lambda v: v <= t["value"],
    }[t["operator"]]
    return [
        {"obs_date": r["obs_date"], "value": r["value"]}
        for r in rows
        if r.get("value") is not None and meets(r["value"])
    ]
