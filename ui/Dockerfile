FROM node:24-slim AS frontend
WORKDIR /frontend
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.9.4 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --extra ui --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --extra ui --no-dev && useradd --create-home --uid 10001 ui
COPY --from=frontend /frontend/dist /app/static
USER ui
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 UI_STATIC_DIR=/app/static ENABLE_SENSITIVE_DATA=false
EXPOSE 8080
CMD ["workplace-ui"]
