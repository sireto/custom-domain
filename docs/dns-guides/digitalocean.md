# Set up DNS at DigitalOcean

Use this guide when your domain is delegated to DigitalOcean's name servers
(`ns1`, `ns2` and `ns3.digitalocean.com`). DigitalOcean hosts DNS but is not
a registrar, so the delegation is set where you bought the domain. If the
domain uses other nameservers, add the records there instead. See
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

1. In the control panel, click **Networking** in the main menu, then
   **Domains**, then the domain `customer.example`.
2. Click **Create a record**, select the record type (**TXT** or
   **CNAME**), fill in the fields, and click **Create Record**. Add each record separately.

## How to type the name

**Hostname** takes the subdomain prefix: "to create www.example.com, enter
www in the field". Type the part before your domain:

| Record | Type | Hostname | TXT Value / Is an alias of |
| --- | --- | --- | --- |
| Ownership | `TXT` | `_custom-domain-challenge.forms` | `custom-domain-verify=<token>` |
| Routing | `CNAME` | `forms` | `acme.edge.example.net.` |

Do not type `forms.customer.example` into **Hostname**; the domain would end
up in the name twice.

## TXT value

Enter `custom-domain-verify=<token>` in **TXT Value** as shown, without
quotation marks, like DigitalOcean's own example (`example_name=example_value`).

## CNAME value

**Is an alias of** accepts `@` or a prefix within your domain. For a target
outside your domain, enter the full name with a period at the end,
`acme.edge.example.net.`, which is how DigitalOcean's forms tell a full name
from a prefix. DigitalOcean documents this rule for other record types, so
check the result: after saving, the record must point at
`acme.edge.example.net`, not `acme.edge.example.net.customer.example`. Enter
a hostname only, without `https://`, and remove any other record for
`forms` first.

## How long it takes

DigitalOcean does not state a time for record changes. New records are
usually visible within minutes. DigitalOcean recommends a low TTL such as
300 seconds so later changes propagate more quickly.

## Check your records

```
dig +short TXT _custom-domain-challenge.forms.customer.example
dig +short CNAME forms.customer.example
```

If a check still fails, the error codes are explained in
[dns-verification.md](../dns-verification.md#diagnostics).

## Sources

Checked against DigitalOcean's documentation on 2026-10-12:

- [How to create, edit and delete DNS records](https://docs.digitalocean.com/products/networking/dns/how-to/manage-records/)
- [Point to DigitalOcean name servers from common domain registrars](https://docs.digitalocean.com/products/networking/dns/getting-started/dns-registrars/)
