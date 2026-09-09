FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app

# The official mcp-clickhouse MCP server, in its own environment.
#
# It is not a dependency of this project on purpose: mcp-clickhouse pulls fastmcp,
# which requires the MCP SDK 2.x, while ADK's McpToolset is built against 1.x. An MCP
# server is a separate process by design, so `uv tool install` gives it its own venv
# and the conflict disappears instead of being pinned around. The version is pinned so
# a rebuild cannot silently change the server the agents talk to.
ENV UV_TOOL_BIN_DIR=/usr/local/bin
RUN uv tool install mcp-clickhouse==0.6.0 \
    && mcp-clickhouse --help >/dev/null 2>&1 || test -x /usr/local/bin/mcp-clickhouse

COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --no-editable --frozen

COPY . .

RUN mkdir -p logs

ENV PYTHONPATH=/app \
    PORT=8080 \
    PYTHONUNBUFFERED=1

# Cloud Run supplies PORT.
CMD ["sh", "-c", "uv run uvicorn web.app:app --host 0.0.0.0 --port ${PORT:-8080}"]
