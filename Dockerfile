# syntax=docker/dockerfile:1.7

FROM node:22-bookworm-slim AS frontend-build
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY frontend/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.12.13@sha256:b485bd65cc2cf1c9a93b3554012c9c3778cf7b1b5fd3d3096ce9e1226c97e1e6 AS uv

FROM python:3.13-slim-bookworm AS python-build
ENV UV_PROJECT_ENVIRONMENT=/opt/oveo-venv \
    UV_LINK_MODE=copy
WORKDIR /build
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable
RUN mkdir -p /build/tiktoken-cache \
    && TIKTOKEN_CACHE_DIR=/build/tiktoken-cache \
       /opt/oveo-venv/bin/python -c 'import tiktoken; tiktoken.get_encoding("o200k_base")'

FROM python:3.13-slim-bookworm AS runtime
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="Oveo" \
      org.opencontainers.image.source="https://github.com/pstngh/oveo" \
      org.opencontainers.image.revision="${VCS_REF}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VIRTUAL_ENV=/opt/oveo-venv \
    PATH="/opt/oveo-venv/bin:$PATH" \
    OVEO_ENVIRONMENT=production \
    OVEO_DATA_DIR=/data \
    OVEO_DATABASE_URL=sqlite+aiosqlite:////data/oveo.sqlite3 \
    OVEO_ATTACHMENTS_DIR=/data/attachments \
    OVEO_FRONTEND_DIR=/app/frontend \
    OVEO_PROMPTS_DIR=/app/prompts \
    TIKTOKEN_CACHE_DIR=/app/tiktoken-cache

RUN groupadd --gid 10001 oveo \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /app oveo \
    && mkdir -p /app /data \
    && chown 10001:10001 /app /data

COPY --from=python-build /opt/oveo-venv/ /opt/oveo-venv/
COPY --from=python-build --chown=10001:10001 /build/tiktoken-cache/ /app/tiktoken-cache/
COPY --from=frontend-build --chown=10001:10001 /build/frontend/dist/ /app/frontend/
COPY --chown=10001:10001 alembic.ini /app/alembic.ini
COPY --chown=10001:10001 migrations/ /app/migrations/
COPY --chown=10001:10001 prompts/ /app/prompts/
COPY --chown=10001:10001 deploy/container-entrypoint.sh /app/container-entrypoint.sh

WORKDIR /app
USER 10001:10001
EXPOSE 8000
ENTRYPOINT ["/app/container-entrypoint.sh"]
CMD ["uvicorn", "oveo.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
