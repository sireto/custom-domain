# Set up DNS at Google Cloud DNS

Use this guide when your domain's DNS is a Cloud DNS public managed zone,
and the domain's registration uses that zone's name servers (shown under
**Registrar setup** on the zone's page). If it uses other nameservers, add
the records there instead. Domains that moved from Google Domains to
Squarespace follow the [Squarespace guide](squarespace.md). See
[the index](README.md) for other providers.

## The two records

Copy the exact values from the application. In the examples the hostname is
`forms.customer.example` in the domain `customer.example`, and the CNAME
target is `acme.edge.example.net`.

| Purpose | Type | Name (full) | Value |
| --- | --- | --- | --- |
| Ownership | `TXT` | `_custom-domain-challenge.<hostname>` | `"custom-domain-verify=<token>"` (with quotes, see below) |
| Routing | `CNAME` | `<hostname>` | `<cname-target>.` (with a trailing dot, see below) |

## Where the records are

1. In the Google Cloud console, go to the **Cloud DNS zones** page.
2. Click the managed zone for `customer.example`.
3. On the **Zone details** page, click **Add standard**.
4. On the **Create record set** page, fill in **DNS name**, **Resource
   record type**, **TTL** and the record's value, then click **Create**.
   Add each record separately.

## How to type the name

In **DNS name**, "enter the subdomain of the DNS zone" (for example `mail`).
Type the part before your domain:

| Record | DNS name | Resource record type | Value |
| --- | --- | --- | --- |
| Ownership | `_custom-domain-challenge.forms` | `TXT` | `"custom-domain-verify=<token>"` |
| Routing | `forms` | `CNAME` | `acme.edge.example.net.` |

Do not type `forms.customer.example` into **DNS name**. Do not type `@`
either: Cloud DNS treats it literally.

## TXT value: use quotes

Cloud DNS says to "enclose each string in quotation marks". Enter the value
as `"custom-domain-verify=<token>"`. The quotes mark where the text starts
and ends; they are not part of the published value, so the check still sees
`custom-domain-verify=<token>`.

## CNAME value: end it with a dot

In **Canonical name**, enter the target followed by a period:
`acme.edge.example.net.`. Cloud DNS treats a name without a trailing dot as
relative to your zone, so without the dot the record could point at
`acme.edge.example.net.customer.example`. Enter a hostname only, without
`https://`. A CNAME cannot share its name with other records, so remove any
other record set for `forms` first.

## How long it takes

Cloud DNS's own name servers should pick up a change "within 120 seconds".
Resolvers that cached an earlier answer wait for its TTL to expire.

## Check your records

```
dig +short TXT _custom-domain-challenge.forms.customer.example
dig +short CNAME forms.customer.example
```

If a check still fails, the error codes are explained in
[dns-verification.md](../dns-verification.md#diagnostics).

## Sources

Checked against the Cloud DNS documentation on 2026-10-12:

- [Add, modify, and delete records](https://docs.cloud.google.com/dns/docs/records)
- [Records overview](https://docs.cloud.google.com/dns/docs/records-overview)
- [Set up DNS records for a domain name](https://docs.cloud.google.com/dns/docs/set-up-dns-records-domain-name)
- [Monitor DNS propagation](https://docs.cloud.google.com/dns/docs/monitoring)
