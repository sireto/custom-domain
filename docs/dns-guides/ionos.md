# Set up DNS at IONOS

Use this guide when your domain uses IONOS's nameservers. IONOS says its
name servers "must also be used" for DNS changes to take effect; if the
domain uses another provider's, add the records there instead. See
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

IONOS documents the two records in different places.

**The CNAME** is set on the subdomain:

1. Log in and go to **Domains & SSL**.
2. Click the gear symbol under **Actions** next to `customer.example` and
   select **Manage Subdomains**. Create the subdomain `forms` there if it
   is not listed.
3. Click the gear symbol next to `forms.customer.example` and select
   **DNS**.
4. Click **Add record**, choose **CNAME**, enter `forms` as the host name
   and the target in **Point to**, and click **Save**.

**The TXT record** is set on the domain:

1. In **Domains & SSL**, click the gear symbol under **Actions** next to
   `customer.example` and select **DNS**.
2. Click **Add record** and choose **TXT**.
3. Enter **Host name** and **Value** (below) and click **Save**.

## How to type the name

IONOS's examples for the host name are subdomains such as `www` or `shop`:
the part before your domain.

| Record | Type | Host name | Value / Point to |
| --- | --- | --- | --- |
| Ownership | `TXT` | `_custom-domain-challenge.forms` | `custom-domain-verify=<token>` |
| Routing | `CNAME` | `forms` | `acme.edge.example.net` |

Do not type the full name into the host name field. After saving, check that
the record list shows `_custom-domain-challenge.forms.customer.example` and
`forms.customer.example`, not the domain twice.

## TXT value

Enter `custom-domain-verify=<token>` as shown, without quotation marks.

## CNAME value

Enter the target as a hostname only, without `http://` or `https://`. IONOS
says a CNAME conflicts with a TXT, MX, A, AAAA or other CNAME record on the
same name, and shows a warning when a new record conflicts with an active
service. Remove any such record for `forms` first. The TXT record has a
different name (`_custom-domain-challenge.forms`), so it does not conflict.

After the CNAME is added, IONOS email addresses and other IONOS products no
longer work on `forms.customer.example`.

## How long it takes

IONOS applies changes immediately and uses a one-hour TTL; it says a change
can take up to an hour to take effect everywhere, and up to 72 hours in
exceptional cases.

## Check your records

```
dig +short TXT _custom-domain-challenge.forms.customer.example
dig +short CNAME forms.customer.example
```

If a check still fails, the error codes are explained in
[dns-verification.md](../dns-verification.md#diagnostics).

## Sources

Checked against the IONOS help center on 2026-10-12:

- [Configuring a CNAME record for a subdomain](https://www.ionos.com/help/domains/configuring-cname-records-for-subdomains/configuring-a-cname-record-for-a-subdomain/)
- [Managing TXT records](https://www.ionos.com/help/domains/configuring-txt-and-srv-records/managing-txt-records/)
- [Avoiding DNS conflicts](https://www.ionos.com/help/domains/general-information-about-dns-settings/avoiding-dns-conflicts/)
- [Time required for DNS changes](https://www.ionos.com/help/domains/general-information-about-dns-settings/time-required-for-dns-changes/)
