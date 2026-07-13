FROM ghcr.io/astral-sh/uv:0.11.6@sha256:b1e699368d24c57cda93c338a57a8c5a119009ba809305cc8e86986d4a006754 AS uv

FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf
COPY --from=uv /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/app/.venv/bin:$PATH
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY core_agent ./core_agent
COPY memory_service ./memory_service
RUN uv sync --frozen --no-dev && \
    useradd --create-home --uid 10001 agent && \
    mkdir -p /data/durable /tmp/core-agent/runs && \
    chown -R agent:agent /app /data /tmp/core-agent

USER agent
STOPSIGNAL SIGTERM
EXPOSE 8000
CMD ["core-agent"]
