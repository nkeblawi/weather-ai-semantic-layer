"""
The MCP server's settings, built and checked once at startup (load). Warehouse
credentials stay in the environment, where wxcore.db reads them; only their
names are checked here, never their values.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# what wxcore.db reads to reach the warehouse; checked by name only
REQUIRED = (
    "WX_CATALOG",
    "DATABRICKS_SERVER",
    "DATABRICKS_HTTP",
    "DATABRICKS_CLIENT",
    "DATABRICKS_SECRET",
)

# Limits: protect the Free Edition warehouse quota the app and ingest jobs share.
# Env name -> default; tune without a code change
LIMITS = {
    "WX_MCP_USER_PER_MINUTE": 6,
    "WX_MCP_USER_PER_DAY": 50,
    "WX_MCP_TOTAL_PER_DAY": 500,
}

# optional: a file of users refused every call (wx_mcp/access/blocklist.py)
BLOCKLIST = "WX_MCP_BLOCKLIST"

# optional sign-in (wx_mcp/access/auth.py): both or neither. ISSUER is the OAuth provider's issuer;
# RESOURCE is this server's MCP URL, which tokens must name as their audience
ISSUER = "WX_MCP_ISSUER"
RESOURCE = "WX_MCP_RESOURCE"
LOCAL_HOSTS = {
    "localhost",
    "127.0.0.1",
}

# optional - server listening address and port. localhost by default; container sets 0.0.0.0
HOST = "WX_MCP_HOST"
PORT = "WX_MCP_PORT"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


@dataclass(frozen=True)
class Settings:
    user_per_minute: int
    user_per_day: int
    total_per_day: int
    blocklist: Path | None = None
    issuer: str | None = None
    resource: str | None = None
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT


class SettingsError(Exception):
    pass


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------


def load() -> Settings:
    """Settings from the environment; raises SettingsError listing every problem."""
    problems = []
    # credentials: present, by name only
    if missing := [name for name in REQUIRED if not os.environ.get(name, "").strip()]:
        problems.append(f"set {', '.join(missing)} in the environment or .env")
    # limits: whole numbers above 0, or the default
    values = {}
    for name, default in LIMITS.items():
        raw = os.environ.get(name, "").strip()
        try:
            values[name] = int(raw) if raw else default
        except ValueError:
            values[name] = None
        if values[name] is None or values[name] < 1:
            problems.append(f"{name} must be a whole number above 0")
    # blocklist: optional, but a path that's set must exist (a typo would block no one)
    blocklist = None
    if raw := os.environ.get(BLOCKLIST, "").strip():
        blocklist = Path(raw).expanduser().resolve()
        if not blocklist.is_file():
            problems.append(f"{BLOCKLIST} names a file that doesn't exist")
    # sign-in: both or neither, so the server can't start half-protected; https except on this machine
    issuer, resource = (
        os.environ.get(name, "").strip() or None for name in (ISSUER, RESOURCE)
    )
    if bool(issuer) != bool(resource):
        problems.append(f"set both {ISSUER} and {RESOURCE}, or neither")
    for name, url in ((ISSUER, issuer), (RESOURCE, resource)):
        if url and not _allowed_url(url):
            problems.append(f"{name} must be an https URL (http only for localhost)")
    # listening address: optional host, and a port from 1 to 65535
    host = os.environ.get(HOST, "").strip() or DEFAULT_HOST
    raw_port = os.environ.get(PORT, "").strip()
    port = (
        int(raw_port)
        if raw_port.isdigit()
        else (DEFAULT_PORT if not raw_port else None)
    )
    if port is None or not 1 <= port <= 65535:
        problems.append(f"{PORT} must be a port number from 1 to 65535")
    if problems:
        raise SettingsError("; ".join(problems))
    settings = Settings(
        *values.values(),
        blocklist=blocklist,
        issuer=issuer,
        resource=resource,
        host=host,
        port=port,
    )
    # a smaller cap above a larger one could never be reached
    if not settings.user_per_minute <= settings.user_per_day <= settings.total_per_day:
        raise SettingsError(
            "limits must satisfy WX_MCP_USER_PER_MINUTE <= WX_MCP_USER_PER_DAY <= WX_MCP_TOTAL_PER_DAY"
        )
    return settings


def _allowed_url(url: str) -> bool:
    parts = urlsplit(url)
    return bool(parts.hostname) and (
        parts.scheme == "https"
        or (parts.scheme == "http" and parts.hostname in LOCAL_HOSTS)
    )
