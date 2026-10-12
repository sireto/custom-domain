# Set up DNS at Namecheap

Use this guide when your domain uses Namecheap BasicDNS, PremiumDNS or
FreeDNS. Namecheap's **Host Records** section is only available for those;
if the domain uses other nameservers (including Namecheap's web hosting,
where records are edited in cPanel), add the records there instead. See
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

1. Select **Domain List** in the left sidebar and click **Manage** next to
   the domain.
2. Select the **Advanced DNS** tab.
3. In the **Host Records** section, click **Add New Record**, choose the
   type (**TXT Record** or **CNAME Record**) and fill in **Host** and
   **Value**.
4. Click **Save All Changes**.

## How to type the name

Namecheap asks for "only the name of your subdomain into the Host field
without adding the domain itself". Type only the part before your domain:

| Record | Type | Host | Value |
| --- | --- | --- | --- |
| Ownership | `TXT Record` | `_custom-domain-challenge.forms` | `custom-domain-verify=<token>` |
| Routing | `CNAME Record` | `forms` | `acme.edge.example.net` |

Do not type `forms.customer.example` into **Host**; the domain would end up
in the name twice.

## TXT value

Enter `custom-domain-verify=<token>` as shown, without quotation marks.

## CNAME value

Enter the target as a hostname only, without `https://`. Namecheap says to
remove conflicting CNAME, URL Redirect, ALIAS or A records on the same host,
so delete any other record for `forms` first.

## How long it takes

Namecheap's default TTL is 30 minutes, and it says to allow about 30 minutes
for new host records to take effect.

## Check your records

```
dig +short TXT _custom-domain-challenge.forms.customer.example
dig +short CNAME forms.customer.example
```

If a check still fails, the error codes are explained in
[dns-verification.md](../dns-verification.md#diagnostics).

## Sources

Checked against Namecheap's knowledge base on 2026-10-12:

- [How do I set up host records for a domain?](https://www.namecheap.com/support/knowledgebase/article.aspx/434/2237/how-do-i-set-up-host-records-for-a-domain/)
- [How to create a CNAME record for your domain](https://www.namecheap.com/support/knowledgebase/article.aspx/9646/2237/how-to-create-a-cname-record-for-your-domain/)
- [How do I add TXT/SPF/DKIM/DMARC records for my domain?](https://www.namecheap.com/support/knowledgebase/article.aspx/317/2237/how-do-i-add-txtspfdkimdmarc-records-for-my-domain/)
