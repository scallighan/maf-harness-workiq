FROM ghcr.io/astral-sh/uv:0.9.4 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev && useradd --create-home --uid 10001 agent
USER agent
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 ENABLE_SENSITIVE_DATA=false
EXPOSE 8088
CMD ["workplace-agent"]
