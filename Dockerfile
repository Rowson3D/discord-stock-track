FROM python:3.13-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    TRACKER_DATA_DIR=/data

WORKDIR /app

# Dependencies first (cached unless the lockfile changes), then the project itself.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project

COPY tracker ./tracker
COPY alembic.ini ./
RUN uv sync --locked --no-dev

RUN useradd --create-home --shell /usr/sbin/nologin stockbot \
    && mkdir -p /data \
    && chown -R stockbot:stockbot /data

USER stockbot

CMD ["tracker", "run"]
