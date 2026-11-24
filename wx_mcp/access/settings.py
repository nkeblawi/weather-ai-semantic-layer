"""
The MCP server's settings, built and checked once at startup (load). Warehouse
credentials stay in the environment, where wxcore.db reads them; only their
names are checked here, never their values.
"""

import os
from dataclasses import dataclass
from pathlib import Path

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


@dataclass(frozen=True)
class Settings:
    user_per_minute: int
    user_per_day: int
    total_per_day: int
    blocklist: Path | None = None


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
    if problems:
        raise SettingsError("; ".join(problems))
    settings = Settings(*values.values(), blocklist=blocklist)
    # a smaller cap above a larger one could never be reached
    if not settings.user_per_minute <= settings.user_per_day <= settings.total_per_day:
        raise SettingsError(
            "limits must satisfy WX_MCP_USER_PER_MINUTE <= WX_MCP_USER_PER_DAY <= WX_MCP_TOTAL_PER_DAY"
        )
    return settings
