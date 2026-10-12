# Set up DNS at Netlify

Use this guide when your domain uses Netlify's nameservers (Netlify DNS). If
the domain uses another provider's nameservers, add the records there
instead; records in Netlify have no effect. See [the index](README.md) for
other providers.

## The two records

Copy the exact values from the application. In the examples the hostname is
`forms.customer.example` in the domain `customer.example`, and the CNAME
target is `acme.edge.example.net`.

| Purpose | Type | Name (full) | Value |
| --- | --- | --- | --- |
| Ownership | `TXT` | `_custom-domain-challenge.<hostname>` | `custom-domain-verify=<token>` |
| Routing | `CNAME` | `<hostname>` | `<cname-target>` |

Netlify's documentation notes that it hosts records for other services "as
long as you host at least one website with us that uses the domain".

## Where the records are

1. In your Netlify team dashboard, select **DNS** in the left sidebar.
2. Select the domain (the DNS zone) `customer.example`.
3. At the bottom of the **DNS records** section, select **Add new record**.
4. Choose the record type, fill in the remaining fields, and select
   **Save**. Add each record separately.

## How to type the name

Netlify's documentation does not describe the name field. Type the name the
way the form suggests, then check the name the **DNS records** list shows
after saving. It must read exactly:

- `_custom-domain-challenge.forms.customer.example` for the TXT record;
- `forms.customer.example` for the CNAME record.

If the list shows `customer.example` twice
(`forms.customer.example.customer.example`), the form added the domain to a
full name: delete the record and add it again with only the part before
your domain (`_custom-domain-challenge.forms`, `forms`).

Netlify does not edit records in place. To change a value, add a new record
and then delete the old one.

## TXT value

Enter `custom-domain-verify=<token>` as shown, without quotation marks.
Netlify allows TXT values of up to 255 characters; the value fits.

## CNAME value

Enter the target as a hostname only, without `https://`. If `forms` is
assigned to a Netlify site, Netlify created a `NETLIFY` record for that
name. A CNAME cannot share its name with other records, so remove
`forms.customer.example` from the site first.

## How long it takes

Netlify says record changes "may take up to a few hours" to propagate, and
that DNS changes can take up to 48 hours, "though most changes take effect
within a few hours".

## Check your records

```
dig +short TXT _custom-domain-challenge.forms.customer.example
dig +short CNAME forms.customer.example
```

If a check still fails, the error codes are explained in
[dns-verification.md](../dns-verification.md#diagnostics).

## Sources

Checked against Netlify's documentation on 2026-10-12:

- [DNS records](https://docs.netlify.com/manage/domains/configure-domains/dns-records/)
- [Manage DNS records](https://docs.netlify.com/manage/domains/manage-domains/manage-dns-records/)
- [Set up Netlify DNS](https://docs.netlify.com/manage/domains/set-up-netlify-dns/)
