# Hosting on Hetzner Cloud

One server runs the whole service: PostgreSQL, the certificate store, the
API, the worker and the edge. A CX22 (2 vCPU, 4 GB) is enough to start;
the edge is I/O bound, not CPU bound. Budget about fifteen minutes.

## 1. Create the server

In the Hetzner Cloud console, **Servers → Add Server**:

- **Location**: the one closest to your customers' origins.
- **Image**: Ubuntu 24.04 (22.04 and Debian 12 also work).
- **Type**: shared vCPU, CX22 or larger.
- **Networking**: public IPv4 and IPv6. Under **Primary IPs**, create both
  the IPv4 and the IPv6 as separate Primary IPs with auto-delete off: these
  are the addresses customers' CNAMEs resolve to, and both must outlive any
  server you replace later. (A server's default IPv6 is deleted with it;
  only a retained Primary IPv6 can be moved.)
- **Firewall**: create one with inbound rules for TCP 22, TCP 80, TCP 443
  and UDP 443, and attach it. Leave outbound open.
- **SSH key**: yours.
- **Cloud config**: paste [deploy/cloud-init.yaml](../deploy/cloud-init.yaml)
  after editing the values in its `write_files` block: your `ACME_EMAIL`,
  the release to install (`CUSTOM_DOMAIN_VERSION` and `CUSTOM_DOMAIN_REF`
  name the same release and move together; never point them at a branch),
  and `SKIP_FIREWALL=1` since the Hetzner firewall is in front (ufw would
  only duplicate it).

Create the server. Installation runs unattended and takes a few minutes
(Docker, the images, the stack); its log is `/var/log/custom-domain-install.log`.

## 2. Point a name at it

In your DNS, create the name customers will CNAME to, for example
`edge.example.net`: an `A` record to the Primary IPv4 and an `AAAA` record to
the Primary IPv6 (the retained one, not an address that belongs to the
server). This name is the `--cname-target` of every application.

## 3. Check it

SSH in and run:

```
custom-domain doctor
```

Every line should be `OK` except `applications: none yet`. The check named
after your edge name confirms that port 80 at that name reaches this edge;
if it warns, wait for DNS to propagate or check the firewall.

## 4. Onboard the first application

```
custom-domain application create --slug acme --name "Acme" --cname-target edge.example.net
custom-domain origin register --application acme --host app.acme.example --scheme https --port 443
custom-domain origin verify --application acme --host app.acme.example --activate
custom-domain credential issue --application acme --label backend
```

The origin must serve the printed token at
`/.well-known/custom-domain-origin-verification` before `verify` succeeds;
the SDK middleware does this. Give the credential to the application's
backend, which registers customer hostnames through the API or SDK.

## Operating

- **Configuration and secrets**: `/opt/custom-domain/deploy/.env`. Back it up
  with the database; it cannot be regenerated.
- **Backups**: `docs/deployment.md` (database dump and certificate store)
  and `docs/operations.md`. Hetzner Object Storage is S3-compatible and a good
  target for the dumps.
- **Upgrade**: set `CUSTOM_DOMAIN_IMAGE` in `.env` to the new version, then
  `cd /opt/custom-domain/deploy && docker compose -f compose.production.yml pull && docker compose -f compose.production.yml up -d`.
- **Management API**: port 9000 inside the Docker network only. Use the
  `custom-domain` command on the host, or put an authenticated reverse
  proxy in front if applications must reach the API from outside; then also
  open that proxy's port in the Hetzner firewall.
- **Upgrading the installer**: `CUSTOM_DOMAIN_REF` in the cloud config only
  matters at creation; on a running server, upgrades are the `.env` edit
  above.
- **Replacing the server**: create a new one the same way (without new
  Primary IPs), restore the database and `.env`, then move both the Primary
  IPv4 and the Primary IPv6 to it. Customers' DNS does not change. If the
  old server used its own IPv6 instead of a Primary IPv6, update the `AAAA`
  record as part of the switch, or drop it.
