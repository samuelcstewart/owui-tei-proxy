# syntax=docker/dockerfile:1.7
#
# Multi-stage build: uv (builder) + python:slim (runtime).
# Runtime deps come from [project] dependencies in pyproject.toml —
# this Dockerfile names no package, so adding a runtime dep to
# pyproject.toml is the only place the image needs to be touched.
#
# The venv lives at /app/.venv in BOTH stages; the console script's
# shebang is baked at venv creation and must match a runtime path.

# ---- Stage 1: builder -----------------------------------------------------
FROM python:3.15-slim-bookworm@sha256:96599df822d0e1c6bb700014a05c7f3c8cabc6e0f7ff3c4f9f69cee033b4b5d9 AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /uvx /usr/local/bin/

WORKDIR /app

# Project metadata first so the dep layer caches across source changes.
# README is required by hatchling (validates the readme field).
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# `--no-editable` is required: uv's default is a .pth file pointing
# back to /app/src, which doesn't exist in the runtime stage.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ---- Stage 2: runtime -----------------------------------------------------
FROM python:3.15-slim-bookworm@sha256:96599df822d0e1c6bb700014a05c7f3c8cabc6e0f7ff3c4f9f69cee033b4b5d9 AS runtime

# Skip groupadd/useradd: they dynamically link libaudit.so.1, which
# crashes under QEMU-user-static emulation on arm64 hosts. Writing
# /etc/passwd and /etc/group directly avoids the dynamic link.
RUN printf 'proxy:x:1001:1001:proxy:/app:/usr/sbin/nologin\n' >> /etc/passwd \
 && printf 'proxy:x:1001:\n'                          >> /etc/group

WORKDIR /app

COPY --from=builder --chown=proxy:proxy /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# NOTE: the listen port is pinned here in the CMD. Kubernetes injects
# PORT=tcp://<cluster-ip>:<port> into every pod via service-links; the
# app ignores that value (see config.py) and always listens on 8000.
EXPOSE 8000

CMD ["uvicorn", "owui_tei_proxy.main:app", "--host", "0.0.0.0", "--port", "8000"]

# Non-root: prevents accidental writes to /app (RO root is a common
# container-hardening pattern; a write attempt fails fast).
USER proxy
