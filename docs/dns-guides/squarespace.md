# Set up DNS at Squarespace

Use this guide when your domain is registered at Squarespace Domains
(including domains that moved there from Google Domains) or connected with
Nameserver Connect. If the domain uses DNS Connect or other nameservers,
Squarespace's records "won't apply to your domain": add them where its DNS
is hosted. See [the index](README.md) for other providers.

## The two records

Copy the exact values from the application. In the examples the hostname is
`forms.customer.example` in the domain `customer.example`, and the CNAME
target is `acme.edge.example.net`.

| Purpose | Type | Name (full) | Value |
| --- | --- | --- | --- |
| Ownership | `TXT` | `_custom-domain-challenge.<hostname>` | `custom-domain-verify=<token>` |
| Routing | `CNAME` | `<hostname>` | `<cname-target>` |

## Where the records are

1. Open the domains dashboard and click the domain name.
2. Click **DNS**, then **DNS Settings**.
3. Scroll down to **Custom Records** and click **Add record**. Squarespace
   may ask for your password or two-factor code first.
4. Choose the record type in **Type**, fill in the fields, and click
   **Save**. Add each record separately.

## How to type the name

Squarespace "automatically adds your domain to the Name field", so type only
the part before your domain:

| Record | Type | Name | Text / Data |
| --- | --- | --- | --- |
| Ownership | `TXT` | `_custom-domain-challenge.forms` | `custom-domain-verify=<token>` |
| Routing | `CNAME` | `forms` | `acme.edge.example.net` |

Do not type `forms.customer.example` into **Name**: the domain would be
added a second time and the check would never find the record.

## TXT value

Enter `custom-domain-verify=<token>` in **Text** without quotation marks and
without a full stop at the end. Squarespace warns that quotation marks in
TXT data can cause problems.

## CNAME value

Enter the target in **Data** as a hostname only: Squarespace does not allow
slashes or colons, so no `https://`. If Squarespace reports a conflicting
record or a conflicting domain forwarding rule for `forms`, remove the
conflict (**Replace record**, or delete the forwarding rule under **Manage
rules**) and save again. Leave Squarespace's default records for your site
alone; they belong to other names.

If the domain came from Google Domains, Squarespace asks for all records on
one name to use the same TTL. Keep the default TTL.

## How long it takes

Squarespace says "it can take 24 to 48 hours" for a change to update. Most
new records are visible much sooner.

## Check your records

```
dig +short TXT _custom-domain-challenge.forms.customer.example
dig +short CNAME forms.customer.example
```

If a check still fails, the error codes are explained in
[dns-verification.md](../dns-verification.md#diagnostics).

## Sources

Checked against Squarespace's help center on 2026-10-12:

- [DNS records for web hosting](https://support.squarespace.com/hc/en-us/articles/31119879125645-DNS-records-for-web-hosting)
- [Adding TXT records](https://support.squarespace.com/hc/en-us/articles/31120980444429-Adding-TXT-records)
- [DNS records for email](https://support.squarespace.com/hc/en-us/articles/31120985010957-DNS-records-for-email)
- [Edit your domain's DNS records](https://support.squarespace.com/hc/en-us/articles/360002101888-Edit-your-domain-s-DNS-records)
- [Troubleshooting issues with DNS records](https://support.squarespace.com/hc/en-us/articles/31110082258573-Troubleshooting-issues-with-DNS-records)
- [DNS record conflicts](https://support.squarespace.com/hc/en-us/articles/42180412940429-DNS-record-conflicts)
