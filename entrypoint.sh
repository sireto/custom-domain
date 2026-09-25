#!/bin/bash
# Container roles (CONTAINER_ROLE):
#   all    - single container: Caddy, migrations, API with in-process workers (default,
#            development and small single-instance deployments)
#   api    - migrations and the API; workers disabled unless enabled explicitly
#   worker - lifecycle checks, edge reconciliation and webhook delivery
#   edge   - Caddy (as the caddy user) plus the validating configuration gateway;
#            no database access. Caddy's admin API stays on localhost:2020 and only
#            the gateway on :2019 is reachable from other containers.
# Process separation: caddy owns the certificate store and bootstrap config,
# app runs everything Python with the Redis credentials removed from its
# environment. See docs/deployment.md and docs/operations.md.
set -euo pipefail
cd /app

ROLE="${CONTAINER_ROLE:-all}"
DATA_HOME="${XDG_DATA_HOME:-/var/lib/custom-domain}"
CADDY_HOME="$DATA_HOME/caddy"
BOOTSTRAP=/etc/caddy/bootstrap.json

run_as() {
    local user="$1"
    shift
    setpriv --reuid="$user" --regid="$user" --init-groups "$@"
}

start_caddy() {
    mkdir -p "$CADDY_HOME" /etc/caddy
    chown -R caddy:caddy "$CADDY_HOME" /etc/caddy
    chmod 700 "$CADDY_HOME" /etc/caddy
    custom-domain edge bootstrap --output "$BOOTSTRAP" >/dev/null
    chown caddy:caddy "$BOOTSTRAP"
    chmod 600 "$BOOTSTRAP"
    run_as caddy env HOME="$CADDY_HOME" XDG_DATA_HOME="$DATA_HOME" \
        XDG_CONFIG_HOME="$CADDY_HOME/.config" caddy start --config "$BOOTSTRAP"
}

# exec cannot call a shell function, so the privilege drop is spelled out on
# each exec line: run as app with the Redis credentials removed.
AS_APP=(setpriv --reuid=app --regid=app --init-groups env
        -u CADDY_REDIS_PASSWORD -u CADDY_REDIS_USERNAME -u CADDY_REDIS_ENCRYPTION_KEY
        -u CADDY_REDIS_TLS_SERVER_CERTS_PEM HOME=/app)

mkdir -p /app/data /app/domains
chown -R app:app /app/data /app/domains

case "$ROLE" in
  all)
    start_caddy
    exec "${AS_APP[@]}" bash -c \
        'custom-domain db upgrade && exec uvicorn app.main:app --host 0.0.0.0 --port 9000'
    ;;
  api)
    export DNS_WORKER_ENABLED="${DNS_WORKER_ENABLED:-false}"
    export WEBHOOK_WORKER_ENABLED="${WEBHOOK_WORKER_ENABLED:-false}"
    export EDGE_RECONCILE_ENABLED="${EDGE_RECONCILE_ENABLED:-false}"
    exec "${AS_APP[@]}" bash -c \
        'custom-domain db upgrade && exec uvicorn app.main:app --host 0.0.0.0 --port 9000'
    ;;
  worker)
    exec "${AS_APP[@]}" custom-domain worker run
    ;;
  edge)
    # Caddy's admin API listens on 127.0.0.1:2020 in this container; the gateway
    # on :2019 is what the reconciler talks to.
    export CADDY_ADMIN_URL="${CADDY_ADMIN_URL:-http://127.0.0.1:2020}"
    start_caddy
    exec "${AS_APP[@]}" custom-domain edge gateway \
        --listen 0.0.0.0:2019 --caddy-admin "$CADDY_ADMIN_URL" --api-url "${API_URL:?set API_URL}"
    ;;
  *)
    echo "unknown CONTAINER_ROLE: $ROLE" >&2
    exit 64
    ;;
esac
