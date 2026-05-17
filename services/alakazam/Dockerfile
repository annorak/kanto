# syntax=docker/dockerfile:1.7
#
# Multi-stage build:
#
# 1. ``builder`` resolves the uv workspace, builds a virtualenv with
#    alakazam + kanto-commons installed in non-editable mode, and
#    discards uv afterwards.
# 2. ``runtime`` is python:3.12-slim with the venv copied in. No
#    package manager, no compilers, no build artifacts. Runs as the
#    rootless 65532:65532 user the AKS security context expects.
#
# The same image is used for the consumer Deployment and the species-
# centroid CronJob — the difference is the entrypoint command (set
# in the Helm chart).
#
# Build with:
#   docker build -t kantodevacr.azurecr.io/kanto-alakazam:$(git rev-parse HEAD) \
#       -f services/alakazam/Dockerfile .
#
# (Build context is the repo root so the uv workspace files are visible.)

ARG PYTHON_VERSION=3.12.6

# -----------------------------------------------------------------------------
# Stage 1 — builder
# -----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.5.4 /uv /uvx /usr/local/bin/

WORKDIR /workspace

COPY pyproject.toml uv.lock README.md /workspace/
COPY libs/kanto-commons/pyproject.toml libs/kanto-commons/README.md /workspace/libs/kanto-commons/
COPY libs/kanto-commons/src /workspace/libs/kanto-commons/src
COPY infrastructure/migrations/pyproject.toml /workspace/infrastructure/migrations/
COPY infrastructure/migrations/src /workspace/infrastructure/migrations/src
COPY infrastructure/migrations/alembic.ini /workspace/infrastructure/migrations/
COPY services/alakazam/pyproject.toml services/alakazam/README.md /workspace/services/alakazam/
COPY services/alakazam/src /workspace/services/alakazam/src

ENV UV_PROJECT_ENVIRONMENT=/opt/venv
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync \
        --frozen \
        --no-dev \
        --package alakazam \
        --no-editable

# -----------------------------------------------------------------------------
# Stage 2 — runtime
# -----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && apt-get purge -y --auto-remove \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 65532 nonroot \
 && useradd  --system --uid 65532 --gid 65532 --no-create-home --shell /sbin/nologin nonroot

COPY --from=builder /opt/venv /opt/venv

USER 65532:65532
WORKDIR /home/nonroot

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/healthz').read()" || exit 1

ENTRYPOINT ["alakazam"]
