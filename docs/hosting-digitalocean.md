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
  values in its `write_files` block: your `ACME_EMAIL` and the image version
  to run. Keep `SKIP_FIREWALL=0` unless you attach a Cloud Firewall (below).
- **Networking**: enable IPv6.

Create the Droplet. Installation runs unattended and takes a few minutes;
its log is `/var/log/custom-domain-install.log`.

Then, under **Networking → Reserved IPs**, create a Reserved IP and assign
it to the Droplet. This is the address customers' CNAMEs resolve to, and it
must outlive any Droplet you replace later.

Optionally, under **Networking → Firewalls**, create a Cloud Firewall with
inbound SSH, HTTP (80), HTTPS (443) and a custom UDP 443 rule, and apply it
to the Droplet. With it in place, set `SKIP_FIREWALL=1` in the cloud config
before creating the Droplet, or leave ufw on; both together also work.

## 2. Point a name at it

In your DNS (DigitalOcean's or elsewhere), create the name customers will
CNAME to, for example `edge.example.net`: an `A` record to the Reserved IP
and an `AAAA` record to the Droplet's IPv6 address. This name is the
`--cname-target` of every application.

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
  and `docs/operations.md`. Spaces is S3-compatible and a good target for
  the dumps.
- **Managed databases**: DigitalOcean's managed PostgreSQL and Valkey work
  in place of the bundled containers. Point `DATABASE_URL` at the managed
  PostgreSQL and set `CADDY_REDIS_ADDRESS`, `CADDY_REDIS_USERNAME`,
  `CADDY_REDIS_PASSWORD` and `CADDY_REDIS_TLS=true` for Valkey, and remove
  the `db` and `redis` services from the Compose file.
- **Upgrade**: set `CUSTOM_DOMAIN_IMAGE` in `.env` to the new version, then
  `cd /opt/custom-domain/deploy && docker compose -f compose.production.yml pull && docker compose -f compose.production.yml up -d`.
- **Management API**: port 9000 inside the Docker network only. Use the
  `custom-domain` command on the host, or put an authenticated reverse
  proxy in front if applications must reach the API from outside.
- **Replacing the Droplet**: create a new one the same way, restore the
  database and `.env`, and reassign the Reserved IP. Customers' DNS does
  not change.
