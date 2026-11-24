"""
Per-user and total call limits for the MCP server's tools, in this process's
memory (like the CSV store: a second process would keep its own counts).
configure() is called once in server.main(); until then nothing is limited.
"""

import threading
import time
from collections import deque
from datetime import UTC, datetime
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver.exceptions import ToolError
from wx_mcp.access.settings import Settings

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WINDOW_SECONDS = 60
LOCAL_USER = "local"  # no sign-in yet (local runs): every call shares one count


# ---------------------------------------------------------------------------
# Limiter
# ---------------------------------------------------------------------------


class Limiter:
    """Counts each allowed call; a call over any limit raises ToolError and isn't counted."""

    def __init__(self, settings: Settings, clock=time.time):
        self.settings, self.clock = settings, clock
        self.lock = threading.Lock()
        self.day = None
        self.recent: dict[str, deque] = {}  # user -> times of calls in the last minute
        self.today: dict[str, int] = {}  # user -> calls today
        self.total = 0

    def check(self, user: str) -> None:
        with self.lock:
            now = self.clock()
            # a new UTC day starts every count over
            day = datetime.fromtimestamp(now, UTC).date()
            if day != self.day:
                self.day, self.recent, self.today, self.total = day, {}, {}, 0
            # drop calls older than the window
            recent = self.recent.setdefault(user, deque())
            while recent and now - recent[0] >= WINDOW_SECONDS:
                recent.popleft()
            s = self.settings
            if self.total >= s.total_per_day:
                raise ToolError(
                    "this server's shared daily limit is used up; tell the user to try again tomorrow "
                    "(it resets at midnight UTC), and don't retry today"
                )
            if self.today.get(user, 0) >= s.user_per_day:
                raise ToolError(
                    f"the daily limit of {s.user_per_day} tool calls per user is reached; tell the user to "
                    "try again tomorrow (it resets at midnight UTC), and don't retry today"
                )
            if len(recent) >= s.user_per_minute:
                raise ToolError(
                    f"too many calls: at most {s.user_per_minute} tool calls a minute; wait a minute before the next one"
                )
            recent.append(now)
            self.today[user] = self.today.get(user, 0) + 1
            self.total += 1


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------

_limiter: Limiter | None = None


def configure(settings: Settings) -> None:
    global _limiter
    _limiter = Limiter(settings)


def current_user() -> str:
    """The signed-in user as issuer plus subject, or LOCAL_USER with no token."""
    token = get_access_token()
    if token is None or not token.subject:
        return LOCAL_USER
    return f"{(token.claims or {}).get('iss', '')}|{token.subject}"


def check() -> None:
    if _limiter:
        _limiter.check(current_user())
