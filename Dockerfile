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
# pg_dump for GET /operator/v1/backup. It must match the server (postgres:16
# in the Compose files), and Debian's own client is older, so it comes from
# the PostgreSQL project's repository, whose key is pinned by checksum.
ADD --checksum=sha256:0144068502a1eddd2a0280ede10ef607d1ec592ce819940991203941564e8e76 \
    https://www.postgresql.org/media/keys/ACCC4CF8.asc /usr/share/keyrings/pgdg.asc
RUN chmod 0644 /usr/share/keyrings/pgdg.asc \
    && echo "deb [signed-by=/usr/share/keyrings/pgdg.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
        >/etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libcap2-bin postgresql-client-16 \
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
COPY examples ./examples
RUN uv sync --frozen --no-dev \
    && uv pip install --no-deps ./sdk \
    && mkdir -p /app/domains /app/data

VOLUME ["/var/lib/custom-domain"]

CMD ["/app/entrypoint.sh"]
