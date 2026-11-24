"""
Users refused every call: a text file named by WX_MCP_BLOCKLIST, one user per line
(the usage log's "user" value, or just its subject), # for comments. Reread when
the file changes, so a block takes effect on the user's next call.
"""

import logging
import os
import threading
from pathlib import Path
from wx_mcp.access.limits import LimitError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Blocklist
# ---------------------------------------------------------------------------


class Blocklist:
    def __init__(self, path: Path):
        self.path, self.lock = path, threading.Lock()
        self.mtime, self.users = None, frozenset()
        self._reload()

    def _reload(self) -> None:
        """Reread the file if it changed; if it can't be read, keep the last list (still blocking)."""
        try:
            mtime = os.stat(self.path).st_mtime
            if mtime == self.mtime:
                return
            lines = self.path.read_text().splitlines()
        except OSError as exc:
            logger.warning("blocklist %s unreadable, keeping the last list: %s", self.path, exc)
            return
        # one user per line; anything after # is a comment
        self.users = frozenset(u for line in lines if (u := line.split("#", 1)[0].strip()))
        self.mtime = mtime

    def check(self, user: str) -> None:
        with self.lock:
            self._reload()
            # the full key ("issuer|subject") or just the subject
            blocked = user in self.users or user.rsplit("|", 1)[-1] in self.users
        if blocked:
            raise LimitError("this account is blocked from this server; tell the user, and don't retry")


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------

_blocklist: Blocklist | None = None


def configure(path: Path | None) -> None:
    """Called once in server.main(); with no path, nobody is blocked."""
    global _blocklist
    _blocklist = Blocklist(path) if path else None


def check(user: str) -> None:
    if _blocklist:
        _blocklist.check(user)
