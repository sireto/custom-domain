# Set up DNS at any provider

Use this guide when your DNS provider has no guide of its own in
[the index](README.md). The steps are the same everywhere; only the names of
the menus differ.

## The two records

The application shows you two records for your hostname. Copy their values
from there: the token is different for every registration, and the target
belongs to the application.

| Purpose | Type | Name | Value |
| --- | --- | --- | --- |
| Ownership | `TXT` | `_custom-domain-challenge.<hostname>` | `custom-domain-verify=<token>` |
| Routing | `CNAME` | `<hostname>` | `<cname-target>` |

In the examples below the hostname is `forms.customer.example`, the zone (the
domain you bought) is `customer.example`, and the CNAME target is
`acme.edge.example.net`.

## Find where your DNS is hosted

Records only count where your domain's **nameservers** point. That is often,
but not always, the company you bought the domain from. If you moved DNS to
another service (for example a CDN or your web host), add the records there.
To see which nameservers your domain uses:

```
dig +short NS customer.example
```

## Type the name the way your provider wants it

DNS providers handle the name field in one of two ways:

- **Relative names (most providers).** The form shows your domain next to
  the name box, or the help text says "host" or "subdomain". Type only the
  part before your domain:

  | Record | Type this |
  | --- | --- |
  | TXT | `_custom-domain-challenge.forms` |
  | CNAME | `forms` |

- **Full names.** The form asks for the "fully qualified" name. Type the
  whole name, `_custom-domain-challenge.forms.customer.example` and
  `forms.customer.example`. Some providers want a dot at the end
  (`forms.customer.example.`); follow what the form says.

**The common mistake** is typing the full name into a field that adds the
domain itself. The record is then created as
`forms.customer.example.customer.example`, and the check never finds it. If
you are unsure, save the record and look at the name the list shows, or use
the `dig` commands below.

## The TXT value

Enter `custom-domain-verify=<token>` exactly as shown, with no spaces. Do
not add quotation marks unless your provider's form says TXT values must be
quoted. When a provider stores your quotes as part of the text, the value no
longer matches and the check reports `txt_token_mismatch`.

## The CNAME value

Enter the target as a plain hostname, such as `acme.edge.example.net`: no
`https://`, no path. A CNAME cannot share its name with other records, so
remove any `A`, `AAAA` or other `CNAME` record for the same hostname first.
If your provider offers a proxy, CDN or "web forwarding" option for the
record, turn it off: the record must be plain DNS. The application's edge
obtains the certificate for your hostname itself, and it can only do that
when traffic reaches it directly.

## How long it takes

New records are usually visible within minutes, but providers quote up to
48 hours. A change to an existing record can take as long as that record's
old TTL. The service rechecks automatically (after 1, 2, 5, 15 and 30
minutes, then hourly), and the application may offer a "check again"
button.

## Check your records

```
dig +short TXT _custom-domain-challenge.forms.customer.example
dig +short CNAME forms.customer.example
```

The first should print your `custom-domain-verify=...` value (in quotes,
which is how `dig` shows TXT text), and the second your CNAME target. If the
check still fails, the error codes are explained in
[dns-verification.md](../dns-verification.md#diagnostics).
