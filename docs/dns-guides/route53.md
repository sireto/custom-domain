# Set up DNS at Amazon Route 53

Use this guide when your domain's DNS is a Route 53 public hosted zone, and
the domain's registration uses that hosted zone's four name servers. If it
uses other nameservers, add the records there instead. See
[the index](README.md) for other providers.

## The two records

Copy the exact values from the application. In the examples the hostname is
`forms.customer.example` in the domain `customer.example`, and the CNAME
target is `acme.edge.example.net`.

| Purpose | Type | Name (full) | Value |
| --- | --- | --- | --- |
| Ownership | `TXT` | `_custom-domain-challenge.<hostname>` | `"custom-domain-verify=<token>"` (with quotes, see below) |
| Routing | `CNAME` | `<hostname>` | `<cname-target>` |

## Where the records are

1. Open the Route 53 console and choose **Hosted zones** in the navigation
   pane.
2. Choose the hosted zone `customer.example`.
3. Choose **Create record**.
4. Fill in **Record name**, **Record type**, **Value** and **TTL
   (seconds)** with the **Simple routing** policy, then choose **Create
   records**. Add each record separately.

## How to type the name

**Record name** is the subdomain; its default is the hosted zone's name.
Enter the part before your domain:

| Record | Record name | Record type | Value |
| --- | --- | --- | --- |
| Ownership | `_custom-domain-challenge.forms` | `TXT` | `"custom-domain-verify=<token>"` |
| Routing | `forms` | `CNAME` | `acme.edge.example.net` |

After saving, the record list must show
`_custom-domain-challenge.forms.customer.example` and
`forms.customer.example`, not the domain twice.

## TXT value: use quotes

Route 53 is the exception to "no quotes": its documentation says "Enclose
text in quotation marks". Enter the value as
`"custom-domain-verify=<token>"`. The quotes mark where the text starts and
ends; they are not part of the published value, so the check still sees
`custom-domain-verify=<token>`.

## CNAME value

Use a plain **CNAME** record with **Alias** turned off. Route 53 alias
records can only point at selected AWS resources and records in the same
hosted zone, not at the application's edge. Enter the target as a hostname
only, without `https://`; a trailing dot is optional. A CNAME cannot share
its name with any other record, so remove any other record for `forms`
first.

## How long it takes

Route 53 says changes "generally propagate to all Route 53 name servers
within 60 seconds". A changed record can still be cached elsewhere for up to
its old TTL.

## Check your records

```
dig +short TXT _custom-domain-challenge.forms.customer.example
dig +short CNAME forms.customer.example
```

If a check still fails, the error codes are explained in
[dns-verification.md](../dns-verification.md#diagnostics).

## Sources

Checked against the Amazon Route 53 Developer Guide on 2026-10-12:

- [Creating records by using the Amazon Route 53 console](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/resource-record-sets-creating.html)
- [Values for simple records](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/resource-record-sets-values-basic.html)
- [Values that are common for all routing policies](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/resource-record-sets-values-shared.html)
- [Supported DNS record types](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/ResourceRecordTypes.html)
- [Choosing between alias and non-alias records](https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/resource-record-sets-choosing-alias-non-alias.html)
