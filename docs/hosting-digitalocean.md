# Hosting on DigitalOcean

One Droplet runs the whole service: PostgreSQL, the certificate store, the
API, the worker and the edge. A Basic Droplet with 2 GB of memory is enough
to start. Budget about fifteen minutes.

## 1. Create the Droplet

In the DigitalOcean console, **Create → Droplets**:

- **Region**: the one closest to your customers' origins.
- **Image**: Ubuntu 24.04 (22.04 and Debian 12 also work).
- **Size**: Basic, regular CPU, 2 GB or larger.
- **Authentication**: your SSH key.
- **Advanced options → Add initialization scripts (free)**: paste
  [deploy/cloud-init.yaml](../deploy/cloud-init.yaml) after editing the
  values in its `write_files` block: your `ACME_EMAIL`, `EDGE_HOSTNAME`
  (the edge's own name from step 2) and the release to
  install (`CUSTOM_DOMAIN_VERSION`, one version number for the installer,
  the image and the SDK; never a branch). Keep
  `SKIP_FIREWALL=0` unless you attach a Cloud Firewall (below).
- **Networking**: enable IPv6 (a Reserved IPv6 requires it).

Create the Droplet. Installation runs unattended and takes a few minutes;
its log is `/var/log/custom-domain-install.log`.

Then, under **Networking → Reserved IPs**, create a Reserved IPv4 and a
Reserved IPv6 and assign both to the Droplet. These are the addresses
customers' CNAMEs resolve to, and they must outlive any Droplet you replace
later; both can be reassigned to another Droplet in the same datacenter
(Reserved IPv6 is available since June 2025, see
https://docs.digitalocean.com/products/networking/reserved-ips/).

Optionally, under **Networking → Firewalls**, create a Cloud Firewall with
inbound SSH, HTTP (80), HTTPS (443) and a custom UDP 443 rule, and apply it
to the Droplet. With it in place, set `SKIP_FIREWALL=1` in the cloud config
before creating the Droplet, or leave ufw on; both together also work.

## 2. Point a name at it

In your DNS (DigitalOcean's or elsewhere), create the name customers will
CNAME to, for example `edge.example.net`: an `A` record to the Reserved IPv4
and an `AAAA` record to the Reserved IPv6 (the reserved addresses, never the
Droplet's own). This name is the `--cname-target` of every application.

## 3. Check it

SSH in and run:

```
custom-domain doctor
```

Every line should be `OK` except `applications: none yet`. Once an
application exists, the check named after its CNAME target looks the name
up in public DNS (not the server's own resolver, so naming the server after
the edge does not confuse it), then fetches
`https://<target>/.well-known/custom-domain-edge-health` at every published
address with the name as SNI, and passes only when each answers `204` with
this edge's `X-Custom-Domain-Edge: 1` marker. That proves DNS, port 443 and
certificate issuance at once, for IPv4 and IPv6 alike. If it fails: wait for
DNS to propagate, confirm TCP 443 (and 80, which Let's Encrypt uses for the
challenge) is open, and run it again after a minute, since the edge obtains
the certificate for its own name on the first handshake. A `TLS` or
`certificate` error that persists points at issuance: check the edge
container's log for the ACME error and that `ACME_EMAIL` is set.

Or use the portal: with `EDGE_HOSTNAME` and `PORTAL_ALLOWED_IPS` set in the
cloud config, open `https://edge.example.net/portal` once the DNS record
from step 2 resolves (the first visit obtains the certificate, so allow a
moment); otherwise `ssh -N -L 9000:127.0.0.1:9000 root@<server>` and open
http://localhost:9000/portal. The password is `PORTAL_PASSWORD` in
`/opt/custom-domain/deploy/.env`. The doctor and everything in step 4 are
pages there ([portal.md](portal.md)).

## 4. Onboard the first application

```
custom-domain application create --slug acme --name "Acme"      # CNAME target defaults to EDGE_HOSTNAME
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
  and `docs/operations.md`. Spaces is S3-compatible and a good target for
  the dumps.
- **Managed databases**: DigitalOcean's managed PostgreSQL and Valkey work
  in place of the bundled containers. Point `DATABASE_URL` at the managed
  PostgreSQL and set `CADDY_REDIS_ADDRESS`, `CADDY_REDIS_USERNAME`,
  `CADDY_REDIS_PASSWORD` and `CADDY_REDIS_TLS=true` for Valkey, and remove
  the `db` and `redis` services from the Compose file.
- **Upgrade**: `custom-domain upgrade <version>` (re-runs the installer from
  that release: image, new settings, Compose refresh, pull and restart; see
  [deployment.md](deployment.md#upgrading)).
- **Management API**: published on `127.0.0.1:9000` of the host only (the
  SSH tunnel and the `custom-domain` command use it). Put an authenticated
  reverse proxy in front if applications must reach the API from outside.
- **Upgrading the installer**: `CUSTOM_DOMAIN_VERSION` in the user data only
  matters at creation; on a running Droplet, upgrades are the `.env` edit
  above.
- **Replacing the Droplet**: create a new one the same way in the same
  datacenter, restore the database and `.env`, then reassign both the
  Reserved IPv4 and the Reserved IPv6 to it. Customers' DNS does not
  change. Verify with `curl -4 -I` and `curl -6 -I` against
  `https://edge.example.net/.well-known/custom-domain-edge-health` (expect
  `204` and `X-Custom-Domain-Edge: 1` on both) before retiring the old
  Droplet.
