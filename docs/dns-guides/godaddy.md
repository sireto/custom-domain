# Set up DNS at GoDaddy

Use this guide when your domain uses GoDaddy's nameservers. If it uses
another provider's nameservers, add the records there instead. See
[the index](README.md) for other providers.

## The two records

Copy the exact values from the application. In the examples the hostname is
`forms.customer.example` in the domain `customer.example`, and the CNAME
target is `acme.edge.example.net`.

| Purpose | Type | Name (full) | Value |
| --- | --- | --- | --- |
| Ownership | `TXT` | `_custom-domain-challenge.<hostname>` | `custom-domain-verify=<token>` |
| Routing | `CNAME` | `<hostname>` | `<cname-target>` |

## Where the records are

1. Sign in to your GoDaddy **Domain Portfolio**.
2. Select the domain to open its **Domain Settings** page.
3. Select **DNS** to view your DNS records.
4. Select **Add New Record** and choose **TXT** or **CNAME** from the
   **Type** menu.
5. Fill in **Name** and **Value**, leave **TTL** at its default (1 hour),
   and select **Save**. To add both records at once, use **Add More
   Records**, then **Save All Records**.

A domain with Domain Protection asks for an extra verification step.

## How to type the name

GoDaddy's **Name** is "the hostname or prefix of the record, without the
domain name". Type only the part before your domain:

| Record | Type | Name | Value |
| --- | --- | --- | --- |
| Ownership | `TXT` | `_custom-domain-challenge.forms` | `custom-domain-verify=<token>` |
| Routing | `CNAME` | `forms` | `acme.edge.example.net` |

Do not type `forms.customer.example` into **Name**; the domain would end up
in the name twice. GoDaddy also does not allow a full stop at the start or
end of the name.

## TXT value

Enter `custom-domain-verify=<token>` as shown, without quotation marks.

## CNAME value

Enter the target as a hostname only, without `https://`, even though
GoDaddy's form calls it a URL. A CNAME cannot share its name with other
records, so remove any other record for `forms` first.

## How long it takes

GoDaddy says "most DNS changes take effect within an hour but could take up
to 48 hours to update globally."

## Check your records

```
dig +short TXT _custom-domain-challenge.forms.customer.example
dig +short CNAME forms.customer.example
```

If a check still fails, the error codes are explained in
[dns-verification.md](../dns-verification.md#diagnostics).

## Sources

Checked against GoDaddy's help center on 2026-10-12:

- [Add a CNAME record](https://www.godaddy.com/help/add-a-cname-record-19236)
- [Add a TXT record](https://www.godaddy.com/help/add-a-txt-record-19232)
