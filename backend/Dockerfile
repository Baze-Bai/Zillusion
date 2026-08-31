FROM python:3.12-slim AS base

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml constraints.txt ./
# -c constraints.txt: pyproject pins only floors, so an unconstrained build
# resolves against PyPI-of-the-day and can drift into a breaking major between
# two builds of the same commit. constraints.txt is the resolved set for this
# image's platform; regenerate it (see its header) when pyproject changes.
RUN pip install --no-cache-dir -c constraints.txt -e ".[dev]" 2>/dev/null \
    || pip install --no-cache-dir -c constraints.txt .

COPY . .

# The discovery agentic super-node runs via the Claude Agent SDK's bundled
# Claude Code CLI (a self-contained binary — no system Node needed). That CLI
# refuses --dangerously-skip-permissions (which the SDK uses) when running as
# root, so the container MUST run as a non-root user.
RUN useradd -m -u 1000 appuser \
    && mkdir -p /app/agent-workspace \
    && chown -R appuser:appuser /app
USER appuser
ENV HOME=/home/appuser

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
