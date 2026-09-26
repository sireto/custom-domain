# Hosting on Hetzner Cloud

One server runs the whole service: PostgreSQL, the certificate store, the
API, the worker and the edge. A CX22 (2 vCPU, 4 GB) is enough to start;
the edge is I/O bound, not CPU bound. Budget about fifteen minutes.

## 1. Create the server

In the Hetzner Cloud console, **Servers → Add Server**:

- **Location**: the one closest to your customers' origins.
- **Image**: Ubuntu 24.04 (22.04 and Debian 12 also work).
- **Type**: shared vCPU, CX22 or larger.
- **Networking**: public IPv4 and IPv6. Under **Primary IPs**, create the
  IPv4 as a separate Primary IP (not auto-deleted with the server): this is
  the address customers' CNAMEs resolve to, and it must outlive any server
  you replace later.
- **Firewall**: create one with inbound rules for TCP 22, TCP 80, TCP 443
  and UDP 443, and attach it. Leave outbound open.
- **SSH key**: yours.
- **Cloud config**: paste [deploy/cloud-init.yaml](../deploy/cloud-init.yaml)
  after editing the values in its `write_files` block: your `ACME_EMAIL`,
  the image version to run, and `SKIP_FIREWALL=1` since the Hetzner firewall
  is in front (ufw would only duplicate it).

Create the server. Installation runs unattended and takes a few minutes
(Docker, the images, the stack); its log is `/var/log/custom-domain-install.log`.

## 2. Point a name at it

In your DNS, create the name customers will CNAME to, for example
`edge.example.net`: an `A` record to the Primary IPv4 and an `AAAA` record to
the server's IPv6. This name is the `--cname-target` of every application.

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
- **Replacing the server**: create a new one the same way, restore the
  database and `.env`, and move the Primary IP to it. Customers' DNS does
  not change.
