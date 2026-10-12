# Set up DNS at Cloudflare

Use this guide when your domain's DNS is hosted at Cloudflare. See
[the index](README.md) for other providers.

**The CNAME record must be "DNS only" (gray cloud), not "Proxied" (orange
cloud).** Cloudflare turns proxying on by default, so check it on every
record you add. The [explanation](#why-the-cname-must-be-dns-only) is below.

## The two records

Copy the exact values from the application. In the examples the hostname is
`forms.customer.example` in the domain `customer.example`, and the CNAME
target is `acme.edge.example.net`.

| Purpose | Type | Name (full) | Value |
| --- | --- | --- | --- |
| Ownership | `TXT` | `_custom-domain-challenge.<hostname>` | `custom-domain-verify=<token>` |
| Routing | `CNAME` | `<hostname>` | `<cname-target>` |

## Where the records are

1. In the Cloudflare dashboard, select the domain and go to the **DNS
   Records** page.
2. Select **Add record** and choose the record **Type**.
3. Fill in the fields (below) and select **Save**. Add each record
   separately.

To change an existing record, select **Edit** on it, make the change and
select **Save**.

## How to type the name

Cloudflare's examples type only the subdomain in **Name** (`blog` for
`blog.example.com`), with `@` for the domain itself. Type the part before
your domain:

| Record | Type | Name | Content / Target | Proxy status |
| --- | --- | --- | --- | --- |
| Ownership | `TXT` | `_custom-domain-challenge.forms` | `custom-domain-verify=<token>` | (none; TXT is always DNS only) |
| Routing | `CNAME` | `forms` | `acme.edge.example.net` | **DNS only** |

After saving, the record list must show `forms.customer.example`, not
`forms.customer.example.customer.example`.

## TXT value

Enter `custom-domain-verify=<token>` in **Content** without quotation marks.
Cloudflare adds the surrounding double quotes itself when you save TXT
content without them, and it can reject inconsistent quotes.

## CNAME value

Enter the target in **Target** as a hostname only, without `https://`.
Remove any `A`, `AAAA` or other `CNAME` record for `forms` first: a CNAME
cannot share its name with other records.

Then set **Proxy status** to **DNS only** (the gray cloud icon) before
saving, or **Edit** the record afterwards and change it.

## Why the CNAME must be DNS only

With **Proxied**, Cloudflare answers DNS queries for the name with its own
addresses instead of your record: Cloudflare's documentation says proxied
records are "answered with Cloudflare anycast IP addresses", and proxied
CNAME records are flattened. Two things then go wrong:

- **The routing check fails.** The service looks for a CNAME from
  `forms.customer.example` to `acme.edge.example.net`. A proxied record
  shows only Cloudflare's addresses, so the check reports `cname_not_found`
  and the domain stays in `pending_dns`.
- **The certificate cannot be issued.** The application's edge obtains a
  certificate for your hostname itself, and the certificate authority must
  reach the edge directly to confirm it. With proxying on, visitors and the
  certificate authority reach Cloudflare, which intercepts the request and
  serves its own certificate, so the edge never sees them.

With **DNS only**, Cloudflare publishes the CNAME as you typed it and
traffic goes straight to the application's edge, which serves your hostname
over HTTPS with its own certificate.

Do not turn proxying on later either. If you do, the next routine check
fails and the hostname stops being served until the record is DNS only
again.

## How long it takes

Cloudflare's default TTL ("Auto") is 300 seconds. New records usually
resolve within minutes; Cloudflare notes local DNS caches can make it take
longer than five minutes.

## Check your records

```
dig +short TXT _custom-domain-challenge.forms.customer.example
dig +short CNAME forms.customer.example
```

The CNAME query must print `acme.edge.example.net.`. If it prints nothing
while `dig +short forms.customer.example` prints addresses, the record is
still proxied. Other error codes are explained in
[dns-verification.md](../dns-verification.md#diagnostics).

## Sources

Checked against Cloudflare's documentation on 2026-10-12:

- [Create DNS records](https://developers.cloudflare.com/dns/manage-dns-records/how-to/create-dns-records/)
- [Create subdomain records](https://developers.cloudflare.com/dns/manage-dns-records/how-to/create-subdomain/)
- [DNS record types](https://developers.cloudflare.com/dns/manage-dns-records/reference/dns-record-types/)
- [Proxy status](https://developers.cloudflare.com/dns/proxy-status/)
- [Time to Live (TTL)](https://developers.cloudflare.com/dns/manage-dns-records/reference/ttl/)
