FROM ghcr.io/astral-sh/uv:0.11.6 AS uv

FROM mikefarah/yq:4.53.3@sha256:11a1f0b604b13dbbdc662260d8db6f644b22d8553122a25c1b5b2e8713ca6977 AS yq

FROM python:3.12-slim
COPY --from=uv /uv /uvx /bin/
COPY --from=yq /usr/bin/yq /usr/local/bin/yq

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/app/.venv/bin:$PATH
WORKDIR /app

RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' \
        /etc/apt/sources.list.d/debian.sources && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        7zip \
        bzip2 \
        ca-certificates \
        coreutils \
        curl \
        fd-find \
        file \
        findutils \
        gawk \
        grep \
        iproute2 \
        jq \
        libarchive-tools \
        netcat-openbsd \
        openssl \
        poppler-utils \
        qpdf \
        ripgrep \
        sed \
        sqlite3 \
        tree \
        uchardet \
        unzip \
        xmlstarlet \
        xxd \
        xz-utils \
        zip \
        zstd && \
    ln -s /usr/bin/fdfind /usr/local/bin/fd && \
    rm -rf /var/lib/apt/lists/*

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
