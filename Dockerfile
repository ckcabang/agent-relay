# Agent Relay API image. It needs PostgreSQL; compose.yaml runs both:
#   docker compose up -d --build
# or, against an existing database:
#   docker run --rm -p 8000:8000 -e RELAY_DATABASE_URL=postgresql://user:pass@host:5432/db agent-relay:local

FROM python:3.11-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Install locked dependencies first so code changes don't invalidate this layer.
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY *.py dashboard.html ./


FROM python:3.11-slim

RUN useradd --create-home --uid 10001 relay

WORKDIR /app
COPY --from=build --chown=relay:relay /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER relay
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=2).status == 200 else 1)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
