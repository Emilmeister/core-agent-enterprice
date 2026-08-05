FROM ghcr.io/astral-sh/uv:0.11.6 AS uv

FROM mikefarah/yq:4.53.3 AS yq

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
RUN uv sync --frozen --no-dev --no-install-project && \
    uv export --frozen --only-group python-tool --no-emit-project \
        --output-file /tmp/python-tool-requirements.txt && \
    uv pip install --python /usr/local/bin/python3 \
        --require-hashes --no-deps --only-binary :all: \
        --requirements /tmp/python-tool-requirements.txt && \
    rm /tmp/python-tool-requirements.txt && \
    uv cache clean

COPY core_agent ./core_agent
RUN uv sync --frozen --no-dev && \
    uv cache clean && \
    useradd --create-home --uid 10001 agent && \
    mkdir -p /data/durable /tmp/core-agent/runs && \
    chown -R agent:agent /app /data /tmp/core-agent

COPY --chown=0:0 third_party/skills/ /opt/core-agent/skills/
RUN cd /opt/core-agent/skills && \
    sha256sum --check SHA256SUMS && \
    test -z "$(find . -type l -print -quit)" && \
    chmod -R a-w .

ENV SKILLS_ROOT=/opt/core-agent/skills \
    CORE_AGENT_ALLOWED_SKILLS=systematic-debugging,verification-before-completion,knowledge-synthesis,explore-data,validate-data,statistical-analysis,sql-queries

USER agent
STOPSIGNAL SIGTERM
EXPOSE 8000
CMD ["core-agent"]
