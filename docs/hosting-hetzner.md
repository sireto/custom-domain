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
  after editing the values in its `write_files` block: your `ACME_EMAIL`, `EDGE_HOSTNAME`
  (the edge's own name from step 2),
  the release to install (`CUSTOM_DOMAIN_VERSION`, one version number for
  the installer, the image and the SDK; never a branch),
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
  and `docs/operations.md`. Hetzner Object Storage is S3-compatible and a good
  target for the dumps.
- **Upgrade**: `custom-domain upgrade <version>` (re-runs the installer from
  that release: image, new settings, Compose refresh, pull and restart; see
  [deployment.md](deployment.md#upgrading)).
- **Management API**: published on `127.0.0.1:9000` of the host only (the
  SSH tunnel and the `custom-domain` command use it). Put an authenticated
  reverse proxy in front if applications must reach the API from outside;
  then also open that proxy's port in the Hetzner firewall.
- **Upgrading the installer**: `CUSTOM_DOMAIN_VERSION` in the cloud config only
  matters at creation; on a running server, upgrades are the `.env` edit
  above.
- **Replacing the server**: Hetzner configures a Primary IP inside the
  guest automatically only when it is assigned at server creation; a
  Primary IPv6 assigned later must be configured by hand
  (https://docs.hetzner.com/cloud/servers/primary-ips/primary-ip-configuration/).
  So move the addresses by creating the new server with them:
  1. Power off the old server and unassign both Primary IPs from it
     (**Primary IPs → Unassign**; they stay, auto-delete being off).
  2. Create the new server the same way, and under **Networking** choose
     the existing Primary IPv4 and Primary IPv6 instead of new ones. Both
     are then configured automatically in the guest.
  3. Restore the database and `.env` (or let the installer run and then
     restore), and start the stack.
  4. Verify both address families before deleting the old server:
     `curl -4 -I https://edge.example.net/.well-known/custom-domain-edge-health`
     and the same with `-6` must both answer `204` with
     `X-Custom-Domain-Edge: 1`; `custom-domain doctor` reports the same.
  If you assigned a Primary IPv6 to a running server instead, add it to the
  guest's network configuration as the Hetzner page describes (a netplan
  entry for the /64 with the gateway `fe80::1`), apply it, and run the same
  verification. Customers' DNS does not change either way.
