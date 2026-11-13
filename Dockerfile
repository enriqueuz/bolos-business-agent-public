# syntax=docker/dockerfile:1
FROM python:3.13-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /uvx /bin/
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client && rm -rf /var/lib/apt/lists/*

# Sin `RUN --mount=type=cache`: Railway exige un `id` en cada cache mount y rechaza el Dockerfile entero.
# ---- dev: incluye dependencias de test; el código se monta como volumen en compose
FROM base AS dev
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project
COPY . .
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ---- prod: solo dependencias de runtime
FROM base AS prod
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
COPY . .
RUN useradd --create-home --uid 10001 bolos
USER bolos
EXPOSE 8000
# Varios workers de uvicorn son seguros: no hay estado en proceso (todo en Redis/Postgres).
# EXCEPCIÓN: con RUN_WORKER_IN_API=true, cada proceso uvicorn levantaría su propio worker de arq
# embebido — con UVICORN_WORKERS=1 en ese modo, la cola tiene un solo consumidor (igual que separado).
CMD ["sh", "-c", "uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 --workers ${UVICORN_WORKERS:-2} --proxy-headers"]
