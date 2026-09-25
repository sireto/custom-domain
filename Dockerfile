# Caddy with the Redis storage module for shared certificate storage
# (docs/decisions/0001-certificate-storage.md). Pinned to the Caddy release
# the module is tested against.
FROM caddy:2.11.4-builder AS caddy-builder
RUN xcaddy build --with github.com/pberkel/caddy-storage-redis

FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/
COPY --from=caddy-builder /usr/bin/caddy /usr/bin/caddy

# Separate users for Caddy (owns keys) and the API (never reads them); Caddy
# binds :80/:443 without root through a file capability. See entrypoint.sh.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libcap2-bin \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system caddy \
    && useradd --system --gid caddy --home-dir /var/lib/custom-domain/caddy \
        --shell /usr/sbin/nologin caddy \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && setcap cap_net_bind_service=+ep /usr/bin/caddy \
    && mkdir -p /var/lib/custom-domain/caddy /etc/caddy

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

# Dependencies first so they cache independently of application code.
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY sdk ./sdk
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY alembic.ini entrypoint.sh ./
RUN uv sync --frozen --no-dev \
    && mkdir -p /app/domains /app/data

VOLUME ["/var/lib/custom-domain"]

CMD ["/app/entrypoint.sh"]
