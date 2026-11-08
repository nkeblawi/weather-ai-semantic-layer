"""
HTTP for NOAA's APIs: a session that retries transient failures on one call,
and per-source circuit breakers that fail fast during an outage, so a station
loop doesn't pay the full retry cost for every station.

Usage:

    from http_utils import get_session, noaa_breaker

    session = get_session()

    @noaa_breaker
    def fetch(url):
        response = session.get(url, timeout=30)
        response.raise_for_status()
        return response

    response = fetch("https://www.ncei.noaa.gov/pub/data/ghcn/daily/...")
"""

import pybreaker
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --- Per-call retry policy ---
_RETRY_TOTAL = 3
_RETRY_BACKOFF_FACTOR = 1.0  # 1s, 2s, 4s between retries
_RETRY_STATUS_FORCELIST = (500, 502, 503, 504)


def get_session() -> requests.Session:
    """A requests.Session that retries 5xx responses and dropped connections."""
    session = requests.Session()
    retry = Retry(
        total=_RETRY_TOTAL,
        backoff_factor=_RETRY_BACKOFF_FACTOR,
        status_forcelist=_RETRY_STATUS_FORCELIST,
        allowed_methods=("GET", "HEAD"),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# --- Circuit breakers, isolated per data source ---
# Open after 5 failures in a row, for 5 minutes; separate so one outage doesn't block the other source
ghcn_breaker = pybreaker.CircuitBreaker(
    fail_max=5,
    reset_timeout=300,
)

emshr_breaker = pybreaker.CircuitBreaker(
    fail_max=5,
    reset_timeout=300,
)

acis_breaker = pybreaker.CircuitBreaker(
    fail_max=5,
    reset_timeout=300,
)

cdo_breaker = pybreaker.CircuitBreaker(
    fail_max=5,
    reset_timeout=300,
)

geonames_breaker = pybreaker.CircuitBreaker(
    fail_max=5,
    reset_timeout=300,
)
