# Set up DNS at Porkbun

Use this guide when your domain uses Porkbun's nameservers. If it uses
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

1. Log in and open **Domain Management**.
2. Click **DNS** under the domain (or **Details**, then the edit icon under
   **DNS RECORDS**).
3. In **Manage DNS Records**, choose the **Type**, fill in **Host** and
   **Answer**, leave **TTL** at its default, and click **Add**. Add each
   record separately.

## How to type the name

Porkbun's **Host** field takes the subdomain part; it is left blank for the
domain itself. Type only the part before your domain:

| Record | Type | Host | Answer |
| --- | --- | --- | --- |
| Ownership | `TXT` | `_custom-domain-challenge.forms` | `custom-domain-verify=<token>` |
| Routing | `CNAME` | `forms` | `acme.edge.example.net` |

Do not type `forms.customer.example` into **Host**. After saving, the record
list must show `forms.customer.example`, not
`forms.customer.example.customer.example`.

## TXT value

Enter `custom-domain-verify=<token>` as shown, without quotation marks.

## CNAME value

Porkbun requires the answer of a CNAME to be a fully qualified domain name:
enter the target as a hostname only, without `https://`. A CNAME's host
"cannot be shared with other records", so remove any other record for
`forms` first.

If Porkbun answers "A CNAME or ALIAS record with that host already exists",
the domain still has Porkbun's default parking records. Delete the records
that point to `pixie.porkbun.com` and add the CNAME again.

## How long it takes

Porkbun says a new subdomain "should start resolving within 24 hours". Most
new records are visible much sooner.

## Check your records

```
dig +short TXT _custom-domain-challenge.forms.customer.example
dig +short CNAME forms.customer.example
```

If a check still fails, the error codes are explained in
[dns-verification.md](../dns-verification.md#diagnostics).

## Sources

Checked against Porkbun's knowledge base on 2026-10-12:

- [How to add DNS records on Porkbun](https://kb.porkbun.com/article/231-how-to-add-dns-records-on-porkbun)
- ["A CNAME or ALIAS record with that host already exists" error](https://kb.porkbun.com/article/239-cname-alias-record-with-that-host-already-exists-error)
- [How to create a subdomain](https://kb.porkbun.com/article/200-how-to-create-a-subdomain)
