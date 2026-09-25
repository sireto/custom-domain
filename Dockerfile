FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg debian-keyring debian-archive-keyring apt-transport-https \
    && curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg \
    && curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        > /etc/apt/sources.list.d/caddy-stable.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends caddy \
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
