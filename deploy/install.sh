#!/usr/bin/env bash
# Installs or upgrades Custom Domain on an Ubuntu 22.04/24.04 or Debian 12
# server: Docker Engine, the production Compose layout under
# /opt/custom-domain, a configuration with generated secrets, the firewall,
# and a `custom-domain` command on the host.
#
# Running it again is the upgrade path (`custom-domain upgrade <version>`
# does exactly that): the release's Compose file replaces the installed one
# when that one is unmodified (a modified one is kept and the new release's
# copy is written next to it as compose.production.yml.new), settings the
# release needs are added to .env with generated values, the image is moved
# to the requested version, and the stack is pulled and restarted. Existing
# data and secrets are never touched.
#
# Settings (environment variables, or /etc/custom-domain-install.env):
#   CUSTOM_DOMAIN_VERSION   release to install: the image tag and, unless overridden, the
#                           git tag the deploy files are fetched from (default: latest, which
#                           follows main; production pins a version such as 0.3.1). On an
#                           upgrade, only an explicitly given version changes the image.
#   CUSTOM_DOMAIN_REF       git ref to fetch deploy files from instead (default: the
#                           version, or main when the version is latest)
#   CUSTOM_DOMAIN_DIR       install directory (default: /opt/custom-domain)
#   CUSTOM_DOMAIN_SOURCE    local checkout to copy deploy files from instead of downloading
#   ACME_EMAIL              contact for the certificate authority (recommended)
#   EDGE_HOSTNAME           the edge's own DNS name, for example edge.example.net: the
#                           default CNAME target, certified and serving the portal from the
#                           first start (recommended)
#   PORTAL_ALLOWED_IPS      addresses or networks that may open the portal through the
#                           edge (default: the address you are installing from over SSH,
#                           when there is one; empty means SSH tunnel only)
#   SKIP_FIREWALL=1         do not touch ufw (when the provider firewall is used instead)
#   SKIP_DOCKER_INSTALL=1   Docker is already installed and running
set -euo pipefail

if [ -f /etc/custom-domain-install.env ]; then
    set -a
    # shellcheck disable=SC1091
    . /etc/custom-domain-install.env
    set +a
fi

VERSION_GIVEN="${CUSTOM_DOMAIN_VERSION:-}"
VERSION="${CUSTOM_DOMAIN_VERSION:-latest}"
if [ -n "${CUSTOM_DOMAIN_REF:-}" ]; then
    REF="${CUSTOM_DOMAIN_REF}"
elif [ "${VERSION}" = "latest" ]; then
    REF="main"
else
    REF="${VERSION}"   # release tags are plain versions
fi
DIR="${CUSTOM_DOMAIN_DIR:-/opt/custom-domain}"
BIN_DIR="${CUSTOM_DOMAIN_BIN_DIR:-/usr/local/bin}"
SOURCE="${CUSTOM_DOMAIN_SOURCE:-}"
RAW="https://raw.githubusercontent.com/sireto/custom-domain/${REF}"
IMAGE="ghcr.io/sireto/custom-domain:${VERSION}"
COMPOSE_FILE="${DIR}/deploy/compose.production.yml"
COMPOSE="docker compose -f ${COMPOSE_FILE}"
ENV_FILE="${DIR}/deploy/.env"
CHECKSUM_FILE="${DIR}/deploy/.compose.production.yml.installed"

log() { printf '\n==> %s\n' "$*"; }
warn() { printf '\n!!  %s\n' "$*" >&2; }

if [ "$(id -u)" -ne 0 ] && [ "${CUSTOM_DOMAIN_SKIP_ROOT_CHECK:-0}" != "1" ]; then
    echo "run as root (sudo)" >&2
    exit 1
fi

# --- Docker ------------------------------------------------------------------
if [ "${SKIP_DOCKER_INSTALL:-0}" != "1" ] && ! command -v docker >/dev/null 2>&1; then
    log "Installing Docker Engine"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl gnupg >/dev/null
    install -m 0755 -d /etc/apt/keyrings
    # shellcheck disable=SC1091
    . /etc/os-release
    curl -fsSL "https://download.docker.com/linux/${ID}/gpg" -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
        >/etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin >/dev/null
    systemctl enable --now docker >/dev/null 2>&1 || true
fi
docker compose version >/dev/null 2>&1 || { echo "docker compose plugin missing" >&2; exit 1; }

# --- Firewall ----------------------------------------------------------------
if [ "${SKIP_FIREWALL:-0}" != "1" ] && command -v ufw >/dev/null 2>&1; then
    log "Opening SSH, HTTP and HTTPS in ufw"
    ufw allow OpenSSH >/dev/null
    ufw allow 80/tcp >/dev/null
    ufw allow 443/tcp >/dev/null
    ufw allow 443/udp >/dev/null
    ufw --force enable >/dev/null
fi

# --- Compose file ---------------------------------------------------------------
# The installed copy is refreshed from the release unless the operator changed
# it; a changed copy is kept and the release's version is left beside it.
mkdir -p "${DIR}/deploy"
fetch_compose() {
    if [ -n "${SOURCE}" ]; then
        cp "${SOURCE}/deploy/compose.production.yml" "$1"
    else
        curl -fsSL "${RAW}/deploy/compose.production.yml" -o "$1"
    fi
}
checksum() { sha256sum "$1" | cut -d' ' -f1; }
fresh_compose="$(mktemp)"
fetch_compose "${fresh_compose}"
if [ ! -f "${COMPOSE_FILE}" ]; then
    log "Installing the Compose file"
    install -m 0644 "${fresh_compose}" "${COMPOSE_FILE}"
    checksum "${COMPOSE_FILE}" >"${CHECKSUM_FILE}"
elif [ "$(checksum "${COMPOSE_FILE}")" = "$(checksum "${fresh_compose}")" ]; then
    checksum "${COMPOSE_FILE}" >"${CHECKSUM_FILE}"
elif [ -f "${CHECKSUM_FILE}" ] && [ "$(checksum "${COMPOSE_FILE}")" = "$(cat "${CHECKSUM_FILE}")" ]; then
    log "Updating the Compose file to this release (the installed one was unmodified)"
    install -m 0644 "${fresh_compose}" "${COMPOSE_FILE}"
    checksum "${COMPOSE_FILE}" >"${CHECKSUM_FILE}"
else
    install -m 0644 "${fresh_compose}" "${COMPOSE_FILE}.new"
    warn "${COMPOSE_FILE} was modified locally and is kept; this release's file is at ${COMPOSE_FILE}.new. Merge the two (diff them) and run again."
fi
rm -f "${fresh_compose}"

# --- Configuration ----------------------------------------------------------------
secret() { openssl rand -hex "$1"; }
if [ -z "${PORTAL_ALLOWED_IPS+x}" ] && [ -n "${SSH_CONNECTION:-}" ]; then
    # Interactive install over SSH: allow the portal from where the operator is.
    PORTAL_ALLOWED_IPS="${SSH_CONNECTION%% *}"
fi
# Print the value of KEY in .env, or nothing.
env_get() { sed -n "s/^$1=//p" "${ENV_FILE}" | head -1; }
# Add KEY=VALUE to .env when the key is missing (never overwrites).
env_add() {
    if ! grep -q "^$1=" "${ENV_FILE}"; then
        printf '%s=%s\n' "$1" "$2" >>"${ENV_FILE}"
        log "Added $1 to .env"
    fi
}
# Replace the value of KEY in .env (adds it when missing).
env_set() {
    if grep -q "^$1=" "${ENV_FILE}"; then
        sed -i "s|^$1=.*|$1=$2|" "${ENV_FILE}"
    else
        printf '%s=%s\n' "$1" "$2" >>"${ENV_FILE}"
    fi
}

if [ ! -f "${ENV_FILE}" ]; then
    log "Generating configuration and secrets"
    pg_password="$(secret 24)"
    umask 077
    cat >"${ENV_FILE}" <<EOF
# Generated by install.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ). Keep this file private.
DATABASE_URL=postgresql+psycopg://custom_domain:${pg_password}@db:5432/custom_domain
POSTGRES_PASSWORD=${pg_password}
ACME_EMAIL=${ACME_EMAIL:-}
EDGE_HOSTNAME=${EDGE_HOSTNAME:-}
EDGE_ASSERTION_KEYS=1:$(secret 32)
EDGE_TOKEN=$(secret 24)
PORTAL_PASSWORD=$(secret 16)
PORTAL_ALLOWED_IPS=${PORTAL_ALLOWED_IPS:-}
CADDY_REDIS_PASSWORD=$(secret 24)
CADDY_REDIS_ENCRYPTION_KEY=$(secret 32)
CUSTOM_DOMAIN_IMAGE=${IMAGE}
EOF
    umask 022
else
    log "Keeping existing ${ENV_FILE}; adding what this release needs"
    # Settings introduced after the installation was made.
    env_add PORTAL_PASSWORD "$(secret 16)"
    env_add PORTAL_ALLOWED_IPS "${PORTAL_ALLOWED_IPS:-}"
    env_add EDGE_HOSTNAME "${EDGE_HOSTNAME:-}"
    if [ -n "${EDGE_HOSTNAME:-}" ] && [ -z "$(env_get EDGE_HOSTNAME)" ]; then
        env_set EDGE_HOSTNAME "${EDGE_HOSTNAME}"
    fi
    if [ -n "${VERSION_GIVEN}" ]; then
        env_set CUSTOM_DOMAIN_IMAGE "${IMAGE}"
        log "Image set to ${IMAGE}"
    else
        IMAGE="$(env_get CUSTOM_DOMAIN_IMAGE)"
        IMAGE="${IMAGE:-ghcr.io/sireto/custom-domain:latest}"
        log "Keeping image ${IMAGE} (give CUSTOM_DOMAIN_VERSION to change it)"
    fi
fi
edge_hostname="$(env_get EDGE_HOSTNAME)"
portal_ips="$(env_get PORTAL_ALLOWED_IPS)"

# --- Host command ---------------------------------------------------------------
# `custom-domain ...` runs the operator CLI inside the API container;
# `custom-domain upgrade [version]` re-runs this installer from that release.
mkdir -p "${BIN_DIR}"
cat >"${BIN_DIR}/custom-domain" <<EOF
#!/usr/bin/env bash
# Custom Domain on this host. Written by deploy/install.sh.
if [ "\${1:-}" = "upgrade" ]; then
    version="\${2:-}"
    ref="\${version:-main}"; [ "\$ref" = latest ] && ref=main
    tmp="\$(mktemp)"
    curl -fsSL "https://raw.githubusercontent.com/sireto/custom-domain/\${ref}/deploy/install.sh" -o "\$tmp"
    CUSTOM_DOMAIN_VERSION="\$version" CUSTOM_DOMAIN_DIR="${DIR}" exec bash "\$tmp"
fi
exec ${COMPOSE} exec api custom-domain "\$@"
EOF
chmod 0755 "${BIN_DIR}/custom-domain"

# --- Start -------------------------------------------------------------------
log "Pulling ${IMAGE} and starting the stack"
cd "${DIR}/deploy"
${COMPOSE} pull -q
${COMPOSE} up -d --wait --wait-timeout 300

# --- Summary -----------------------------------------------------------------
ip4=""; ip6=""
if command -v ip >/dev/null 2>&1; then
    ip4="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="src") print $(i+1)}' | head -1 || true)"
    ip6="$(ip -6 route get 2606:4700:4700::1111 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="src") print $(i+1)}' | head -1 || true)"
fi
name="${edge_hostname:-edge.example.net}"
cat <<EOF

Custom Domain is running.

  Install directory:  ${DIR}
  Configuration:      ${ENV_FILE}   (secrets; back it up)
  Image:              ${IMAGE}
  This server:        ${ip4:-?}${ip6:+  ${ip6}}
  Edge hostname:      ${edge_hostname:-not set (EDGE_HOSTNAME in .env)}

Next steps
  1. DNS: point ${name} at this server: A ${ip4:-<ipv4>}${ip6:+ and AAAA ${ip6}}.
     That is the name customers CNAME to.
  2. Open the portal (password: PORTAL_PASSWORD in ${ENV_FILE}):
EOF
if [ -n "${edge_hostname}" ] && [ -n "${portal_ips}" ]; then
    echo "       https://${edge_hostname}/portal   from ${portal_ips} (PORTAL_ALLOWED_IPS), once DNS resolves; or"
elif [ -n "${portal_ips}" ]; then
    echo "       https://<edge name>/portal from ${portal_ips} once EDGE_HOSTNAME is set in .env (or an application exists); or"
fi
cat <<EOF
       ssh -N -L 9000:127.0.0.1:9000 root@${ip4:-<server>}   then   http://localhost:9000/portal
     or check from the shell:   custom-domain doctor
  3. Create the first application (in the portal, or):
       custom-domain application create --slug acme --name "Acme"${edge_hostname:+   (CNAME target defaults to ${edge_hostname})}${edge_hostname:- --cname-target ${name}}
     then register and verify its origin and issue a credential.
  4. Upgrade later with:   custom-domain upgrade <version>
     (docs/deployment.md for backups, monitoring and the Compose refresh rules)

The management API and portal listen on 127.0.0.1:9000 of this host only
(plus the edge route above); applications reach the API through a reverse
proxy of your own if they run elsewhere (docs/deployment.md).
EOF
