"""
One JSON line per tool call (who, which tool, outcome, how long) on the
"wx_mcp.usage" logger, for spotting abuse. Never the token or the arguments.
"""

import json
import logging
import sys
from datetime import UTC, datetime

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

logger = logging.getLogger("wx_mcp.usage")
logger.propagate = False  # only through configure()'s handler, never the root logger
OUTCOMES = ("ok", "refused", "error", "crash")


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------


def configure() -> None:
    """Usage lines to stderr, where a host collects them; called once in server.main()."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def record(user: str, tool: str, outcome: str, seconds: float) -> None:
    logger.info(
        json.dumps(
            {
                "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "user": user,
                "tool": tool,
                "outcome": outcome,
                "ms": round(seconds * 1000),
            }
        )
    )
