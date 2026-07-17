# The Python services — API and worker — are one image built from the uv workspace. They run the
# same code (FastAPI app and arq worker), so the same image serves both; compose picks the command.
#
# Multi-stage: the builder carries uv and resolves the locked environment; the runtime carries only
# Python, the virtualenv and the source. The two share a Debian bookworm / Python 3.12 base, so the
# venv's interpreter symlinks stay valid when copied across — the reason both tags say bookworm.
#
# Context is the repo root (see compose): the API depends on engine, schema, db and collector by
# path, and uv.lock pins the whole workspace. `--locked` fails the build if the lock drifted.

FROM python:3.12-slim-bookworm AS builder

# The uv binary, pinned, dropped onto the standard Python base — the same base the runtime uses, so
# the virtualenv's interpreter symlinks stay valid when copied across (a floating uv tag would
# reintroduce exactly the "two developers, two builds" drift the pinned service images avoid).
COPY --from=ghcr.io/astral-sh/uv:0.11.5 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Only the Python workspace members — apps/web is a Node package uv does not know about, and the
# MetaTrader5 wheel does not exist on Linux (ADR-02); its platform marker keeps that from failing.
COPY pyproject.toml uv.lock ./
COPY packages/ packages/
COPY apps/api/ apps/api/
COPY apps/collector/ apps/collector/
COPY apps/executor/ apps/executor/

RUN uv sync --locked --no-dev

FROM python:3.12-slim-bookworm AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY --from=builder /app /app

# `create_app` is a factory so tests and compose can inject a lifespan; uvicorn calls it per worker.
EXPOSE 8000
CMD ["uvicorn", "tradeforge_api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
