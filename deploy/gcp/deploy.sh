#!/usr/bin/env bash
# Custom Domain on one Google Compute Engine VM: PostgreSQL, the certificate
# store, the API, the worker and the Caddy edge, installed by
# deploy/install.sh from the release named by CUSTOM_DOMAIN_VERSION.
#
# Run it in Cloud Shell (the "Open in Cloud Shell" button in
# docs/hosting-gcp.md opens this repository with a tutorial), or anywhere
# gcloud is signed in:
#
#   EDGE_HOSTNAME=edge.example.net ACME_EMAIL=ops@example.net \
#   ADMIN_CIDR=203.0.113.9/32 bash deploy/gcp/deploy.sh
#
# Missing values are asked for. Re-running it is safe: every resource is
# created only when it does not exist yet, and the SSH firewall rules follow
# the admin networks given. It creates, all named after NAME:
# a VPC network with a dual-stack subnet, static external IPv4 and IPv6
# addresses (what customers' CNAMEs resolve to), firewall rules (80 and 443
# from anywhere, SSH from ADMIN_CIDR and Google's IAP range) and the VM.
#
# Settings (environment variables):
#   EDGE_HOSTNAME           the edge's own DNS name, for example edge.example.net
#   ACME_EMAIL              contact for Let's Encrypt
#   ADMIN_CIDR              IPv4 address or network you administer from, e.g. 203.0.113.9/32
#   ADMIN_CIDR_IPV6         optional IPv6 network you administer from, e.g. 2001:db8:1234::/64
#                           (add it when your connection has IPv6: browsers prefer it)
#   CUSTOM_DOMAIN_VERSION   release to install (default below)
#   PROJECT                 Google Cloud project (default: gcloud's configured project)
#   REGION                  default: gcloud's compute/region, else us-central1
#   ZONE                    default: the first zone of REGION
#   MACHINE_TYPE            default: e2-small (2 vCPU, 2 GB)
#   NAME                    resource name prefix, default: custom-domain
set -euo pipefail

VERSION="${CUSTOM_DOMAIN_VERSION:-0.11.0}"
NAME="${NAME:-custom-domain}"
MACHINE_TYPE="${MACHINE_TYPE:-e2-small}"
IAP_RANGE="35.235.240.0/20"   # Google's range for `gcloud compute ssh --tunnel-through-iap`

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
ask() {
    # ask VAR "prompt": keep an existing value, otherwise read one.
    local var="$1" prompt="$2"
    if [ -z "${!var:-}" ]; then
        [ -t 0 ] || die "$var is required"
        read -r -p "$prompt: " "${var?}"
    fi
    [ -n "${!var:-}" ] || die "$var is required"
}

command -v gcloud >/dev/null || die "gcloud is not installed (use Cloud Shell)"
PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}"
[ -n "${PROJECT}" ] || die "no project: set PROJECT or run gcloud config set project <id>"
REGION="${REGION:-$(gcloud config get-value compute/region 2>/dev/null || true)}"
REGION="${REGION:-us-central1}"

ask EDGE_HOSTNAME "Edge hostname, the name customers CNAME to (for example edge.example.net)"
ask ACME_EMAIL "Contact email for Let's Encrypt"
ask ADMIN_CIDR "Your IPv4 address or network for the portal and SSH (for example 203.0.113.9/32)"

[[ "${EDGE_HOSTNAME}" =~ ^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$ ]] \
    || die "EDGE_HOSTNAME is not a DNS name: ${EDGE_HOSTNAME}"
[[ "${ACME_EMAIL}" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]] \
    || die "ACME_EMAIL is not an email address: ${ACME_EMAIL}"
# Let's Encrypt refuses reserved contact domains, and no certificate would ever be issued.
acme_domain="$(printf '%s' "${ACME_EMAIL##*@}" | tr '[:upper:]' '[:lower:]')"
if [[ "${acme_domain}" =~ (^|\.)example\.(com|net|org)$ || "${acme_domain}" =~ \.(example|test|invalid|localhost|local)$ ]]; then
    die "ACME_EMAIL must be a real address; Let's Encrypt refuses ${acme_domain}"
fi
# Not the whole internet: the portal allowlist refuses 0.0.0.0/0.
octet='(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])'
[[ "${ADMIN_CIDR}" =~ ^(${octet}\.){3}${octet}(/([89]|[12][0-9]|3[0-2]))?$ ]] \
    || die "ADMIN_CIDR must be an IPv4 address or a /8 to /32 network: ${ADMIN_CIDR}"
ADMIN_CIDR_IPV6="${ADMIN_CIDR_IPV6:-}"
if [ -n "${ADMIN_CIDR_IPV6}" ]; then
    [[ "${ADMIN_CIDR_IPV6}" =~ ^[0-9a-fA-F]{0,4}(:[0-9a-fA-F]{0,4}){2,7}/(1[6-9]|[2-9][0-9]|1[01][0-9]|12[0-8])$ ]] \
        || die "ADMIN_CIDR_IPV6 must be an IPv6 network with a prefix of /16 to /128: ${ADMIN_CIDR_IPV6}"
fi
PORTAL_ALLOWED="${ADMIN_CIDR}${ADMIN_CIDR_IPV6:+,${ADMIN_CIDR_IPV6}}"
[[ "${VERSION}" =~ ^([0-9]+\.[0-9]+\.[0-9]+|latest)$ ]] \
    || die "CUSTOM_DOMAIN_VERSION must be a release such as 0.6.0, or latest"

gc() { gcloud --project "${PROJECT}" --quiet "$@"; }

# Compute Engine must be enabled before anything else, even the zone lookup:
# on a new project every compute call fails until it is.
log "Enabling the Compute Engine API in ${PROJECT}"
gc services enable compute.googleapis.com
ZONE="${ZONE:-$(gc compute zones list --filter="region:(${REGION})" --format='value(name)' --sort-by=name --limit=1)}"
[ -n "${ZONE}" ] || die "no zone found in region ${REGION}"
SUBNET="${NAME}-${REGION}"

log "Project ${PROJECT}, zone ${ZONE}, release ${VERSION}"

if ! gc compute networks describe "${NAME}" >/dev/null 2>&1; then
    log "Creating network ${NAME}"
    gc compute networks create "${NAME}" --subnet-mode=custom
fi
if ! gc compute networks subnets describe "${SUBNET}" --region "${REGION}" >/dev/null 2>&1; then
    log "Creating dual-stack subnet ${SUBNET}"
    gc compute networks subnets create "${SUBNET}" --network "${NAME}" --region "${REGION}" \
        --range 10.80.1.0/24 --stack-type IPV4_IPV6 --ipv6-access-type EXTERNAL
fi

if ! gc compute addresses describe "${NAME}-ipv4" --region "${REGION}" >/dev/null 2>&1; then
    log "Reserving a static IPv4 address"
    gc compute addresses create "${NAME}-ipv4" --region "${REGION}" --network-tier PREMIUM
fi
if ! gc compute addresses describe "${NAME}-ipv6" --region "${REGION}" >/dev/null 2>&1; then
    log "Reserving a static IPv6 address"
    gc compute addresses create "${NAME}-ipv6" --region "${REGION}" --subnet "${SUBNET}" \
        --ip-version IPV6 --endpoint-type VM
fi
IPV4="$(gc compute addresses describe "${NAME}-ipv4" --region "${REGION}" --format='value(address)')"
IPV6="$(gc compute addresses describe "${NAME}-ipv6" --region "${REGION}" --format='value(address)')"

rule() {
    # rule NAME ALLOW SOURCES: create it, or bring its sources up to date.
    local rule_name="$1" allow="$2" sources="$3"
    if ! gc compute firewall-rules describe "${rule_name}" >/dev/null 2>&1; then
        log "Creating firewall rule ${rule_name}"
        gc compute firewall-rules create "${rule_name}" --network "${NAME}" \
            --direction INGRESS --target-tags "${NAME}" --allow "${allow}" --source-ranges "${sources}"
    else
        gc compute firewall-rules update "${rule_name}" --source-ranges "${sources}"
    fi
}
rule "${NAME}-web" tcp:80,tcp:443,udp:443 0.0.0.0/0
rule "${NAME}-web-ipv6" tcp:80,tcp:443,udp:443 ::/0
rule "${NAME}-ssh" tcp:22 "${ADMIN_CIDR},${IAP_RANGE}"
# Firewall rules take one address family each.
if [ -n "${ADMIN_CIDR_IPV6}" ]; then
    rule "${NAME}-ssh-ipv6" tcp:22 "${ADMIN_CIDR_IPV6}"
elif gc compute firewall-rules describe "${NAME}-ssh-ipv6" >/dev/null 2>&1; then
    log "Removing firewall rule ${NAME}-ssh-ipv6 (no ADMIN_CIDR_IPV6 given)"
    gc compute firewall-rules delete "${NAME}-ssh-ipv6"
fi

if ! gc compute instances describe "${NAME}" --zone "${ZONE}" >/dev/null 2>&1; then
    user_data="$(mktemp)"
    trap 'rm -f "${user_data}"' EXIT
    # The same steps as deploy/cloud-init.yaml.
    cat >"${user_data}" <<EOF
#cloud-config
package_update: true
packages: [ca-certificates, curl]
write_files:
  - path: /etc/custom-domain-install.env
    permissions: "0600"
    content: |
      ACME_EMAIL=${ACME_EMAIL}
      EDGE_HOSTNAME=${EDGE_HOSTNAME}
      CUSTOM_DOMAIN_VERSION=${VERSION}
      PORTAL_ALLOWED_IPS=${PORTAL_ALLOWED}
      PUBLIC_API=true
      SKIP_FIREWALL=1
runcmd:
  - [sh, -c, ". /etc/custom-domain-install.env; ref=\"\$CUSTOM_DOMAIN_VERSION\"; [ \"\$ref\" = latest ] && ref=main; curl -fsSL \"https://raw.githubusercontent.com/sireto/custom-domain/\$ref/deploy/install.sh\" -o /root/custom-domain-install.sh"]
  - [sh, -c, "bash /root/custom-domain-install.sh >/var/log/custom-domain-install.log 2>&1"]
EOF
    log "Creating the VM ${NAME} (${MACHINE_TYPE})"
    # No service account: the VM needs no Google Cloud API access.
    gc compute instances create "${NAME}" --zone "${ZONE}" --machine-type "${MACHINE_TYPE}" \
        --image-family ubuntu-2404-lts-amd64 --image-project ubuntu-os-cloud \
        --boot-disk-size 30GB --boot-disk-type pd-balanced \
        --network-interface "subnet=${SUBNET},stack-type=IPV4_IPV6,address=${IPV4},external-ipv6-address=${IPV6},external-ipv6-prefix-length=96,ipv6-network-tier=PREMIUM" \
        --tags "${NAME}" --no-service-account --no-scopes \
        --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring \
        --metadata-from-file user-data="${user_data}"
else
    log "The VM ${NAME} already exists; nothing to create"
    cat <<EOF
The SSH firewall rules now follow ${PORTAL_ALLOWED}. The portal allowlist
lives on the VM: to change it, set PORTAL_ALLOWED_IPS=${PORTAL_ALLOWED} in
/opt/custom-domain/deploy/.env there and restart the api and worker
(docker compose -f compose.production.yml up -d api worker).
EOF
fi

ssh_cmd="gcloud compute ssh ${NAME} --project ${PROJECT} --zone ${ZONE} --tunnel-through-iap"
cat <<EOF

Custom Domain is installing on ${NAME} (about ten minutes; the log is
/var/log/custom-domain-install.log on the VM).

1. DNS: create these records for ${EDGE_HOSTNAME} in your DNS:
     A     ${IPV4}
     AAAA  ${IPV6}
2. The portal (from ${PORTAL_ALLOWED}), once DNS resolves:
     https://${EDGE_HOSTNAME}/portal
   Password:
     ${ssh_cmd} --command 'sudo grep PORTAL_PASSWORD /opt/custom-domain/deploy/.env'
3. Applications call the API at https://${EDGE_HOSTNAME}/v1 with their credential.
4. Check everything:
     ${ssh_cmd} --command 'sudo custom-domain doctor'

Upgrade later on the VM with: custom-domain upgrade <version>
EOF
