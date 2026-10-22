"""
Shared core for the weather front ends (the Streamlit app in app/ and the
MCP server): station lookup, answer_query calls, dates and units, answer
payloads, query validation, series, and chart specs.

Takes structured parameters and returns data, values, or chart specs. It
never calls Claude, never imports anthropic or streamlit, and knows nothing
about sessions, chat history, or user preferences: station, units, and
dates are always explicit arguments.
"""
