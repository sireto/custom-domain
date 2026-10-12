# Set up DNS at Hostinger

Use this guide when your domain uses Hostinger's nameservers. Hostinger
applies the records in its DNS Zone Editor only while the domain points to
Hostinger's nameservers; if it uses another provider's, add the records
there instead. See [the index](README.md) for other providers.

## The two records

Copy the exact values from the application. In the examples the hostname is
`forms.customer.example` in the domain `customer.example`, and the CNAME
target is `acme.edge.example.net`.

| Purpose | Type | Name (full) | Value |
| --- | --- | --- | --- |
| Ownership | `TXT` | `_custom-domain-challenge.<hostname>` | `custom-domain-verify=<token>` |
| Routing | `CNAME` | `<hostname>` | `<cname-target>` |

## Where the records are

1. Open the Hostinger dashboard (hPanel).
2. In the left sidebar, choose **Domains**, then **DNS**.
3. Select the domain. The DNS Zone Editor opens; choose the record type,
   fill in the fields and add the record. Add each record separately.

## How to type the name

Hostinger's **Name** is the hostname "without the domain name"; "the domain
name is automatically appended". Type only the part before your domain:

| Record | Type | Name | Value / Target |
| --- | --- | --- | --- |
| Ownership | `TXT` | `_custom-domain-challenge.forms` | `custom-domain-verify=<token>` |
| Routing | `CNAME` | `forms` | `acme.edge.example.net` |

Do not type `forms.customer.example` into **Name**: the domain would be
appended a second time and the check would never find the record.

## TXT value

Enter `custom-domain-verify=<token>` as shown, without quotation marks.

## CNAME value

Enter the target as a hostname only, without `https://`. A CNAME cannot
share its name with other records, so remove any `A` or `AAAA` record for
`forms` first.

## How long it takes

Hostinger's default TTL is 14400 seconds (4 hours), and it says to allow "up
to 24 hours" for changes to fully propagate. A new record is usually
visible much sooner; a changed record can take up to its old TTL.

## Check your records

```
dig +short TXT _custom-domain-challenge.forms.customer.example
dig +short CNAME forms.customer.example
```

If a check still fails, the error codes are explained in
[dns-verification.md](../dns-verification.md#diagnostics).

## Sources

Checked against Hostinger's support site on 2026-10-12:

- [How to manage DNS records at Hostinger](https://www.hostinger.com/support/1583249-how-to-manage-dns-records-at-hostinger)
- [How to manage CNAME records on hPanel](https://www.hostinger.com/support/4738777-how-to-manage-cname-records-on-hpanel)
- [How to manage TXT records in hPanel](https://www.hostinger.com/support/1583664-how-to-manage-txt-records-in-hpanel)
