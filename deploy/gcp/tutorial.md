# Deploy Custom Domain on Google Cloud

## Before you start

This creates one Compute Engine VM running the whole service (PostgreSQL,
the certificate store, the API, the worker and the Caddy edge), with a
static IPv4 and IPv6 address, in the project you choose. It takes about
fifteen minutes, most of it waiting for the install.

You need:

- a Google Cloud project with billing enabled;
- a DNS name for the edge, for example `edge.example.net`, in a zone you
  control;
- your own public IPv4 address, to reach the portal (search "what is my IP").

<walkthrough-project-setup billing="true"></walkthrough-project-setup>

## Choose the project

Set the project the VM is created in:

```sh
gcloud config set project <walkthrough-project-id/>
```

## Run the deployment

Replace the three values and run:

```sh
EDGE_HOSTNAME=edge.example.net \
ACME_EMAIL=ops@example.net \
ADMIN_CIDR=203.0.113.9/32 \
bash deploy/gcp/deploy.sh
```

To pick the region, add `REGION=europe-west1` (or any other) in front. The
script prints the addresses to put in DNS when it finishes. Running it again
is safe; it only creates what is missing.

## Point DNS at the VM

In your DNS, create an `A` record and an `AAAA` record for the edge hostname
with the two addresses the script printed. Customers' CNAME records will
point at this name.

## Open the portal

Once the DNS records resolve and the install has finished (about ten
minutes), open `https://<edge hostname>/portal` from the address you gave as
`ADMIN_CIDR`. Read the password with the `gcloud compute ssh ... --command
'sudo grep PORTAL_PASSWORD ...'` line the script printed.

The portal walks you through the first application: its backend (origin),
an API key, and the first customer hostname. Applications call the API at
`https://<edge hostname>/v1`.

## Done

Check the deployment at any time with the `custom-domain doctor` line the
script printed. Upgrade later on the VM with `custom-domain upgrade
<version>`. The full guide is `docs/hosting-gcp.md`.
