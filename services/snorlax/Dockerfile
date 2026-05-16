# syntax=docker/dockerfile:1.7
#
# Multi-stage build:
#
# 1. ``prodigal-builder`` compiles Prodigal from the canonical
#    upstream tarball, pinned to a fixed release tag. Pinning is a
#    DoD requirement — Prodigal's gene-call output is stable across
#    versions in practice but we don't want surprises mid-flight.
# 2. ``app-builder`` resolves the uv workspace and builds a venv
#    with Snorlax + kanto-commons installed in non-editable mode.
# 3. ``runtime`` is python:3.12-slim with the venv and the compiled
#    Prodigal copied in. No build tools, no package manager state.
#    Runs as the rootless 65532:65532 user the AKS security context
#    expects.
#
# Build with:
#   docker build -t kantodevacr.azurecr.io/kanto-snorlax:$(git rev-parse HEAD) \
#       -f services/snorlax/Dockerfile .
#
# (Build context is the repo root so the uv workspace files are
# visible. CI does this automatically.)

ARG PYTHON_VERSION=3.12.6
# Pin Prodigal — see services/snorlax/README.md "Prodigal pinning"
# for why and how to bump.
ARG PRODIGAL_VERSION=2.6.3

# -----------------------------------------------------------------------------
# Stage 1 — Prodigal compiled from upstream
# -----------------------------------------------------------------------------
FROM debian:bookworm-slim AS prodigal-builder
ARG PRODIGAL_VERSION

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp
RUN curl -fsSL \
        "https://github.com/hyattpd/Prodigal/archive/refs/tags/v${PRODIGAL_VERSION}.tar.gz" \
        -o prodigal.tar.gz \
 && tar -xzf prodigal.tar.gz \
 && mv "Prodigal-${PRODIGAL_VERSION}" prodigal \
 && cd prodigal \
 && make INSTALLDIR=/usr/local/bin install \
 && /usr/local/bin/prodigal -v

# -----------------------------------------------------------------------------
# Stage 2 — Python venv with snorlax installed
# -----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS app-builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# Pin uv to match what CI uses; bump in lockstep when the base image
# rolls forward.
COPY --from=ghcr.io/astral-sh/uv:0.5.4 /uv /uvx /usr/local/bin/

WORKDIR /workspace

# Bring in just enough of the workspace for ``uv sync`` to resolve.
# Source comes in afterwards. Keep this list minimal: the more we
# COPY, the more the cache busts on unrelated changes.
COPY pyproject.toml uv.lock README.md /workspace/
COPY libs/kanto-commons/pyproject.toml libs/kanto-commons/README.md /workspace/libs/kanto-commons/
COPY libs/kanto-commons/src /workspace/libs/kanto-commons/src
COPY infrastructure/migrations/pyproject.toml /workspace/infrastructure/migrations/
COPY infrastructure/migrations/src /workspace/infrastructure/migrations/src
COPY infrastructure/migrations/alembic.ini /workspace/infrastructure/migrations/
COPY services/snorlax/pyproject.toml services/snorlax/README.md /workspace/services/snorlax/
COPY services/snorlax/src /workspace/services/snorlax/src

ENV UV_PROJECT_ENVIRONMENT=/opt/venv
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync \
        --frozen \
        --no-dev \
        --package snorlax \
        --no-editable

# -----------------------------------------------------------------------------
# Stage 3 — runtime
# -----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    KANTO_SNORLAX_PRODIGAL_BINARY=/usr/local/bin/prodigal \
    KANTO_SNORLAX_WORK_DIR=/var/snorlax/work

# Runtime needs ca-certificates for httpx (NCBI HTTPS) and the Azure
# SDK TLS; that's it. No build tools, no compilers.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && apt-get purge -y --auto-remove \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 65532 nonroot \
 && useradd  --system --uid 65532 --gid 65532 --no-create-home --shell /sbin/nologin nonroot

# Copy the prebuilt venv and Prodigal binary.
COPY --from=app-builder /opt/venv /opt/venv
COPY --from=prodigal-builder /usr/local/bin/prodigal /usr/local/bin/prodigal

# Scratch directory for in-flight genomes. tmpfs-mounted by the pod
# spec (see helm chart) so we don't burn the node disk; the dir is
# owned by the non-root user.
RUN mkdir -p /var/snorlax/work \
 && chown -R 65532:65532 /var/snorlax

USER 65532:65532
WORKDIR /home/nonroot

# Liveness check hits the in-process /healthz endpoint. Does NOT
# probe Mew/Modal — liveness must stay local.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/healthz').read()" || exit 1

ENTRYPOINT ["snorlax"]
