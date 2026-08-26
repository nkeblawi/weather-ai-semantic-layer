"""
Shared HTTP utilities for calling NOAA's APIs (ACIS, GHCN-D bulk file server,
CDO) with retry and circuit-breaker resilience.

Two layers of protection, each solving a different problem:

- urllib3 Retry (via the requests Session's HTTPAdapter): handles transient
  failures on a SINGLE call, e.g. a 503 or a dropped connection. Retries a
  few times with backoff before giving up on that one request.

- pybreaker CircuitBreaker: handles the case where NOAA is down or
  unreachable for an extended period ACROSS MANY calls. After too many
  failures in a row, the breaker "opens" and fails fast (raises immediately)
  for a cooldown window, instead of letting every station in a loop hang or
  retry against a service that's clearly not responding. This matters here
  specifically because a daily ingest job loops over multiple stations
  (KIAD, KDCA, ...) — without a breaker, one NOAA outage would mean every
  station in that loop pays the full retry cost individually.

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
    """
    Return a requests.Session configured with retry-on-failure for
    transient errors (5xx responses, connection drops).
    """
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
# Each opens after 5 consecutive failures; stays open (fails fast) for 5
# minutes before allowing a trial call through again. Kept separate so an
# outage on one source doesn't fail-fast calls to the other.
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
