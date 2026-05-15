# syntax=docker/dockerfile:1.7
#
# Multi-stage build:
#
# 1. ``builder`` resolves the uv workspace, builds a virtualenv with
#    growlithe + kanto-commons installed in non-editable mode, and
#    discards uv afterwards.
# 2. ``runtime`` is python:3.12-slim with the venv copied in. No
#    package manager, no compilers, no build artifacts. Runs as the
#    rootless 65532:65532 user the AKS security context expects.
#
# Build with:
#   docker build -t kantodevacr.azurecr.io/kanto-growlithe:$(git rev-parse HEAD) \
#       -f services/growlithe/Dockerfile .
#
# (Build context is the repo root so the uv workspace files are
# visible. CI does this automatically.)

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

# Install uv into /usr/local/bin/uv. Pinning matches the version
# already used by CI; bump in lockstep with CI when the image base
# changes.
COPY --from=ghcr.io/astral-sh/uv:0.5.4 /uv /uvx /usr/local/bin/

WORKDIR /workspace

# Copy the minimum set of workspace files needed for `uv sync` to
# resolve the dependency graph reproducibly. We bring the full source
# in afterwards.
COPY pyproject.toml uv.lock README.md /workspace/
COPY libs/kanto-commons/pyproject.toml libs/kanto-commons/README.md /workspace/libs/kanto-commons/
COPY libs/kanto-commons/src /workspace/libs/kanto-commons/src
COPY infrastructure/migrations/pyproject.toml /workspace/infrastructure/migrations/
COPY infrastructure/migrations/src /workspace/infrastructure/migrations/src
COPY infrastructure/migrations/alembic.ini /workspace/infrastructure/migrations/
COPY services/growlithe/pyproject.toml services/growlithe/README.md /workspace/services/growlithe/
COPY services/growlithe/src /workspace/services/growlithe/src

# Build a venv at /opt/venv with the growlithe service installed
# non-editable. ``--no-dev`` keeps test deps out of the runtime image;
# ``UV_PROJECT_ENVIRONMENT`` points uv at /opt/venv instead of the
# default ``.venv`` so the runtime stage can copy a known path.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync \
        --frozen \
        --no-dev \
        --package growlithe \
        --no-editable

# -----------------------------------------------------------------------------
# Stage 2 — runtime
# -----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}"

# Drop apt-managed dpkg state so there's nothing for an attacker to
# pivot through. ca-certificates is needed for httpx/azure SDK TLS.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && apt-get purge -y --auto-remove \
 && rm -rf /var/lib/apt/lists/*

# Rootless. UID/GID matches the kanto-common podSecurityContext
# (runAsUser/runAsGroup/fsGroup all 65532).
RUN groupadd --system --gid 65532 nonroot \
 && useradd  --system --uid 65532 --gid 65532 --no-create-home --shell /sbin/nologin nonroot

# Copy the prebuilt venv from the builder stage. Owning it as root
# (read-only to nonroot) keeps the runtime user from rewriting its
# own runtime.
COPY --from=builder /opt/venv /opt/venv

USER 65532:65532
WORKDIR /home/nonroot

# Health check verifies the in-process /healthz endpoint responds.
# It does NOT depend on Mew or Event Hubs — liveness must stay local.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/healthz').read()" || exit 1

ENTRYPOINT ["growlithe"]
