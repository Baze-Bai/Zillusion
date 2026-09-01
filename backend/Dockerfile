FROM python:3.12-slim AS base

# The image mirrors the REPOSITORY layout — /app/backend, /app/harness,
# /app/scripts — rather than flattening backend to /app. That is not cosmetic:
# services/harness_orchestrator.py locates the bridge at
# `Path(__file__).parents[2].parent / "scripts"`, so with backend flattened to
# /app it resolved to /scripts and the whole build stage was unreachable from
# the container. Mirroring the repo makes every relative path in the codebase
# mean the same thing inside and outside Docker.
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY backend/pyproject.toml backend/constraints.txt /app/backend/
COPY harness/pyproject.toml harness/constraints.txt /app/harness/

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
RUN pip install --no-cache-dir -c backend/constraints.txt -e ./backend

# The harness shares this interpreter instead of carrying a venv of its own.
# The two dependency sets resolve together with no conflict (checked with
# `uv pip compile backend/pyproject.toml harness/pyproject.toml`), and one env
# means the orchestrator's subprocess spawn needs no second interpreter to
# find — see HARNESS_PYTHON below and _resolve_harness_python in the bridge.
#
# Its SOURCE is copied before the install, unlike backend's above. An editable
# install resolves `packages = [...]` at install time, so installing against a
# directory holding only pyproject.toml records an empty package map and writes
# no .pth — `import runtime` then fails from anywhere except the harness dir
# itself. The orchestrator happens to spawn with cwd=HARNESS_DIR so it would
# have worked by luck; this makes it work by construction. Cost is one layer of
# cache: editing harness source re-runs this install.
COPY harness/ /app/harness/
RUN pip install --no-cache-dir -c harness/constraints.txt -e ./harness

# Chromium + its system libraries, for the explore/validate/run stages. `--with-deps`
# needs root, so it runs before the USER switch. PLAYWRIGHT_BROWSERS_PATH puts the
# download somewhere every user can read, rather than in root's home where appuser
# could not reach it.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN python -m playwright install --with-deps chromium \
    && chmod -R a+rx /ms-playwright

COPY backend/ /app/backend/
COPY scripts/ /app/scripts/

# The discovery agentic super-node runs via the Claude Agent SDK's bundled
# Claude Code CLI (a self-contained binary — no system Node needed). That CLI
# refuses --dangerously-skip-permissions (which the SDK uses) when running as
# root, so the container MUST run as a non-root user.
RUN useradd -m -u 1000 appuser \
    && mkdir -p /app/backend/agent-workspace /app/harness/workspaces /app/harness/inputs \
    && chown -R appuser:appuser /app
USER appuser
ENV HOME=/home/appuser

# The bridge's resolution order tries $HARNESS_PYTHON first. Setting it makes the
# shared-interpreter arrangement explicit rather than relying on the sys.executable
# fallback, so a broken spawn points at a name you can grep for.
ENV HARNESS_PYTHON=/usr/local/bin/python

WORKDIR /app/backend

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
