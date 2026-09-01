FROM python:3.12-slim AS base

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml constraints.txt ./

# torch FIRST, from PyTorch's CPU index, because on Linux the PyPI `torch` wheel
# is the CUDA build and drags the whole GPU stack in behind it. Measured in this
# image before this line existed: nvidia/ 2.7G + torch/ 1.2G + triton/ 691M —
# about 4.6G of a 10.1G image, none of which can ever run, since the container
# has no GPU and compose sets up no device passthrough.
#
# Installing it here means the main install below finds `torch` already
# satisfied and never asks PyPI for it, so the nvidia pins in constraints.txt
# stay inert (a constraints file caps versions; it does not pull anything in).
#
# If you DO want GPU inference, drop this line and rebuild — the plain PyPI
# resolve gives you the CUDA build — and add the device reservation to
# docker-compose.yml, which is what actually makes it reachable.
RUN pip install --no-cache-dir torch==2.13.0 --index-url https://download.pytorch.org/whl/cpu

# -c constraints.txt: pyproject pins only floors, so an unconstrained build
# resolves against PyPI-of-the-day and can drift into a breaking major between
# two builds of the same commit. constraints.txt is the resolved set for this
# image's platform; regenerate it (see its header) when pyproject changes.
RUN pip install --no-cache-dir -c constraints.txt -e ".[dev]"

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
