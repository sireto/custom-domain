# Caddy with the Redis storage module for shared certificate storage
# (docs/decisions/0001-certificate-storage.md). Pinned to the Caddy release
# the module is tested against.
FROM caddy:2.11.4-builder AS caddy-builder
RUN xcaddy build --with github.com/pberkel/caddy-storage-redis

FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/
COPY --from=caddy-builder /usr/bin/caddy /usr/bin/caddy

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

# Dependencies first so they cache independently of application code.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY alembic.ini entrypoint.sh ./
RUN uv sync --frozen --no-dev \
    && mkdir -p /app/domains /app/data

CMD ["/app/entrypoint.sh"]
