FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY pyproject.toml ./
COPY knappy ./knappy
COPY slack ./slack
# Read from the repo root next to the package (knappy.mcp.servers.DEFAULT_PATH); `python -m` imports /app/knappy.
COPY mcp_servers.toml ./

RUN pip install --no-cache-dir .

# The OAuth callback (KNAPPY_CALLBACK_PORT). Route KNAPPY_PUBLIC_URL here.
EXPOSE 8080

# Socket Mode dials out, and MCP tool lists are cached in memory. Run exactly one worker.
CMD ["python", "-m", "knappy.main"]
