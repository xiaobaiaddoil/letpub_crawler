ARG PYTHON_BASE_IMAGE=public.ecr.aws/docker/library/python:3.12-slim
ARG PLAYWRIGHT_BASE_IMAGE=mcr.microsoft.com/playwright/python:v1.57.0-noble
ARG UV_VERSION=0.11.15

FROM ${PYTHON_BASE_IMAGE} AS web

ARG UV_VERSION

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    UV_LINK_MODE=copy \
    UV_CACHE_DIR=/root/.cache/uv \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock ./

RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=cache,target=/root/.cache/uv \
    python -m pip install --disable-pip-version-check "uv==${UV_VERSION}" \
    && uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY tools ./tools
COPY worker.py logging.conf ./
COPY config ./config
COPY docker/entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh && mkdir -p /app/logs

ENTRYPOINT ["/entrypoint.sh"]
CMD ["web"]


FROM ${PLAYWRIGHT_BASE_IMAGE} AS worker

ARG UV_VERSION

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    UV_LINK_MODE=copy \
    UV_CACHE_DIR=/root/.cache/uv \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock ./

RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=cache,target=/root/.cache/uv \
    python -m pip install --disable-pip-version-check "uv==${UV_VERSION}" \
    && uv sync --frozen --no-dev --no-install-project --extra crawler

COPY app ./app
COPY tools ./tools
COPY worker.py logging.conf ./
COPY config ./config
COPY docker/entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh && mkdir -p /app/logs

ENTRYPOINT ["/entrypoint.sh"]
CMD ["worker"]


FROM web AS runtime
