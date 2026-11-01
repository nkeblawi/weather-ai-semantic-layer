"""
The prompts: shortcuts a user starts from their client's menu or slash commands.
"""

from mcp.server import MCPServer

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EXAMPLES = """\
Show me these examples of what I can ask, as they are, then ask what I'd like to know.

Past weather at stations in the US and Canada (temperature, rain, snow, wind; no forecasts). Values are in °F and inches.

Questions:
- What was the hottest day in Chicago this past summer?
- How much snow did Dulles Airport get in its last snowstorm over 6 inches?
- How many days did it rain in Miami in 2025?

Charts (daily values up to a year, monthly, yearly or seasonal values, counts of days, climatologies, comparisons with the 1991-2020 normals, trend lines):
- Plot average high and low temperature for each month in Chicago
- Chart monthly temperatures in New York City in 2025 against normal
- Graph the number of 90-degree days per year in Washington, DC
"""


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------


def examples() -> str:
    """Example questions and charts to try."""
    return EXAMPLES


def register(mcp: MCPServer) -> None:
    mcp.prompt()(examples)
