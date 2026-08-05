FROM ghcr.io/astral-sh/uv:0.11.6 AS uv

FROM python:3.12-slim
COPY --from=uv /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/app/.venv/bin:$PATH
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY core_agent ./core_agent
RUN uv sync --frozen --no-dev && \
    useradd --create-home --uid 10001 agent && \
    mkdir -p /data/durable /tmp/core-agent/runs && \
    chown -R agent:agent /app /data /tmp/core-agent

USER agent
STOPSIGNAL SIGTERM
EXPOSE 8000
CMD ["core-agent"]
