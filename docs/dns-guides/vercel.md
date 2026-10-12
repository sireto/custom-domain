# Set up DNS at Vercel

Use this guide when your domain uses Vercel's nameservers (Vercel DNS). If
the domain uses another provider's nameservers, add the records there
instead; Vercel's records have no effect. See [the index](README.md) for
other providers.

## The two records

Copy the exact values from the application. In the examples the hostname is
`forms.customer.example` in the domain `customer.example`, and the CNAME
target is `acme.edge.example.net`.

| Purpose | Type | Name (full) | Value |
| --- | --- | --- | --- |
| Ownership | `TXT` | `_custom-domain-challenge.<hostname>` | `custom-domain-verify=<token>` |
| Routing | `CNAME` | `<hostname>` | `<cname-target>` |

The subdomain does not need to be added to a Vercel project. Vercel's
documentation says a subdomain can point to a service hosted elsewhere just
by adding the records that service provides.

## Where the records are

1. On your team's dashboard, open **Domains** in the sidebar.
2. Click the domain to open its **Advanced Settings** page.
3. Fill in the DNS record form (**Name**, **Type**, **Value**, and
   optionally **TTL** and **Comment**) and click **Add**. Add each record
   separately.

## How to type the name

Vercel's **Name** field takes only the prefix, not the full name. Its
documentation says, for a subdomain pointing to an external service: "enter
only `support`, not `support.example.com`".

| Record | Name | Type | Value |
| --- | --- | --- | --- |
| Ownership | `_custom-domain-challenge.forms` | `TXT` | `custom-domain-verify=<token>` |
| Routing | `forms` | `CNAME` | `acme.edge.example.net` |

Do not type the full name (`forms.customer.example`) into **Name**: the
field is the part before your domain, so the domain would end up in the
record's name twice and the check would never find it.

You can also use the Vercel CLI, which takes the domain and the prefix
separately:

```
vercel dns add customer.example _custom-domain-challenge.forms TXT 'custom-domain-verify=<token>'
vercel dns add customer.example forms CNAME acme.edge.example.net
```

## TXT value

Enter `custom-domain-verify=<token>` as shown. Vercel's documentation does
not ask for quotation marks, so do not add any. In the CLI, the single
quotes above are for your shell only.

## CNAME value

Enter the target as a hostname only, without `https://`. Vercel's
documentation says not to add both an A record and a CNAME record for the
same hostname; remove any A record for `forms` first.

## How long it takes

The default TTL is 60 seconds. Vercel says a new record "can take up to 24
hours" to fully update and for local caches to clear.

## Check your records

```
dig +short TXT _custom-domain-challenge.forms.customer.example
dig +short CNAME forms.customer.example
```

If a check still fails, the error codes are explained in
[dns-verification.md](../dns-verification.md#diagnostics).

## Sources

Checked against Vercel's documentation on 2026-10-12:

- [Managing DNS records](https://vercel.com/docs/domains/managing-dns-records)
- [vercel dns (CLI)](https://vercel.com/docs/cli/dns)
- [Create a DNS record (REST API)](https://vercel.com/docs/rest-api/dns/create-a-dns-record)
