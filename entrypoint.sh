#!/bin/bash
# Process separation inside the container:
#   caddy  - runs Caddy; owns the certificate store and the bootstrap config
#            (admin listener plus storage credentials).
#   app    - runs migrations and the API; cannot read either, and has the
#            Redis credentials removed from its environment.
# The API manages only the "apps" subtree of Caddy's configuration through
# the admin API on localhost. See docs/operations.md, "Private key boundaries".
set -euo pipefail
cd /app

DATA_HOME="${XDG_DATA_HOME:-/var/lib/custom-domain}"
CADDY_HOME="$DATA_HOME/caddy"
BOOTSTRAP=/etc/caddy/bootstrap.json

mkdir -p "$CADDY_HOME" /etc/caddy /app/data /app/domains
chown -R caddy:caddy "$CADDY_HOME" /etc/caddy
chmod 700 "$CADDY_HOME" /etc/caddy
chown -R app:app /app/data /app/domains

custom-domain edge bootstrap --output "$BOOTSTRAP" >/dev/null
chown caddy:caddy "$BOOTSTRAP"
chmod 600 "$BOOTSTRAP"

run_as() {
    local user="$1"
    shift
    setpriv --reuid="$user" --regid="$user" --init-groups "$@"
}

run_as caddy env HOME="$CADDY_HOME" XDG_DATA_HOME="$DATA_HOME" XDG_CONFIG_HOME="$CADDY_HOME/.config" \
    caddy start --config "$BOOTSTRAP"

exec setpriv --reuid=app --regid=app --init-groups env \
    -u CADDY_REDIS_PASSWORD -u CADDY_REDIS_USERNAME -u CADDY_REDIS_ENCRYPTION_KEY \
    -u CADDY_REDIS_TLS_SERVER_CERTS_PEM HOME=/app \
    bash -c 'custom-domain db upgrade && exec uvicorn app.main:app --host 0.0.0.0 --port 9000'
