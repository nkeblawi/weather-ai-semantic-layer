# The weather MCP server (wx_mcp + wxcore). Build from the repo root:
#   docker build -t weather-mcp .
# Secrets come from the environment at run time (--env-file), never from the image.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WX_MCP_HOST=0.0.0.0 \
    WX_MCP_PORT=8000

WORKDIR /srv

# The server's pins first, so code changes don't reinstall them
COPY wx_mcp/requirements.txt wx_mcp/requirements.txt
RUN pip install --no-cache-dir -r wx_mcp/requirements.txt

# wxcore and its dependencies (pyproject.toml)
COPY pyproject.toml ./
COPY wxcore wxcore
RUN pip install --no-cache-dir .

COPY wx_mcp wx_mcp

# Fail the build if the server can't import (stands in for the local guard tests)
RUN python -c "import wx_mcp.server"

# Run as a user without root
RUN useradd --create-home --uid 1000 wx
USER wx

EXPOSE 8000
CMD ["python", "-m", "wx_mcp.server"]
