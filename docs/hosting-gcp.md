# Hosting on Google Cloud

One Compute Engine VM runs the whole service: PostgreSQL, the certificate
store, the API, the worker and the edge.
[deploy/gcp/deploy.sh](../deploy/gcp/deploy.sh) creates everything with
`gcloud` and installs the release you choose. Budget about fifteen minutes.

## 1. Deploy

[![Open in Cloud Shell](https://gstatic.com/cloudssh/images/open-btn.svg)](https://shell.cloud.google.com/cloudshell/editor?cloudshell_git_repo=https%3A%2F%2Fgithub.com%2Fsireto%2Fcustom-domain&cloudshell_git_branch=main&cloudshell_tutorial=deploy%2Fgcp%2Ftutorial.md&show=terminal)

The button opens this repository in Cloud Shell with a step-by-step tutorial.
Google Cloud has no one-click equivalent of a CloudFormation or ARM template
outside its Marketplace, so the tutorial runs the script. Anywhere `gcloud`
is signed in, the same command works:

```
EDGE_HOSTNAME=edge.example.net ACME_EMAIL=ops@example.net ADMIN_CIDR=203.0.113.9/32 \
  bash deploy/gcp/deploy.sh
```

`ADMIN_CIDR` is your public IPv4 address (search "what is my IP"), or a
network of `/8` to `/32`; it may open the portal and connect with SSH. If
your connection has IPv6, also set `ADMIN_CIDR_IPV6` (for example
`2001:db8:1234::/64`): your browser prefers the edge's IPv6 address once the
`AAAA` record exists, and the portal admits only listed addresses. Add `REGION=europe-west1` (or another
region) and `MACHINE_TYPE=e2-medium` to change the defaults
(`us-central1`, or gcloud's configured region, and `e2-small`). The script is
safe to re-run; it creates only what is missing and brings the SSH firewall
rules in line with the admin networks given. The portal allowlist lives in
`/opt/custom-domain/deploy/.env` on the VM; the script prints how to change
it there.

What it creates, all named `custom-domain`: a VPC network with a dual-stack
subnet, static external IPv4 and IPv6 addresses, firewall rules (TCP 80 and
443 and UDP 443 from anywhere, SSH from your address and from Google's IAP
range for `gcloud compute ssh --tunnel-through-iap`), and an Ubuntu 24.04 VM
with Shielded VM and no service account (it needs no Google Cloud API
access).

## 2. Point the name at it

The script prints the records: an `A` and an `AAAA` record for the edge
hostname.

## 3. Open the portal

Once the name resolves and the installation has finished (about ten
minutes), open `https://edge.example.net/portal` from your address. The
script prints the command that reads the password:

```
gcloud compute ssh custom-domain --zone <zone> --tunnel-through-iap \
  --command 'sudo grep PORTAL_PASSWORD /opt/custom-domain/deploy/.env'
```

The same with `--command 'sudo custom-domain doctor'` checks everything. The
portal walks you through the first application; applications call the API
at `https://edge.example.net/v1`.

## Operating

- **Upgrade**: `sudo custom-domain upgrade <version>` on the VM
  ([deployment.md](deployment.md#upgrading)).
- **Backups**: `docs/deployment.md` (database dump and certificate store);
  Cloud Storage is a good target.
- **Removing it**: delete the VM, the two addresses, the firewall rules and
  the network (`gcloud compute instances delete custom-domain`, then
  `gcloud compute addresses delete custom-domain-ipv4 custom-domain-ipv6`,
  and so on). Back up first; customers' CNAMEs then point nowhere.
