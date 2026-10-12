# DNS setup guides

Step-by-step instructions for publishing a domain's two records at a specific
DNS provider. Applications can link their customers here; each guide is
addressed to the person who manages the domain's DNS.

Every guide uses the same placeholders as the [v1 API](../api-v1.md):

| Placeholder | Example | Where it comes from |
| --- | --- | --- |
| `<hostname>` | `forms.customer.example` | The hostname registered with `POST /v1/domains` |
| zone | `customer.example` | The domain the customer bought; its DNS zone |
| `<token>` | `Qm9vayBvZiB0aGUg...` | The TXT value `custom-domain-verify=<token>` in `dns_records[]` with `purpose: ownership` |
| `<cname-target>` | `acme.edge.example.net` | `dns_records[]` with `purpose: routing` |

The records themselves, and what each check verifies, are described in
[dns-verification.md](../dns-verification.md).

## Guides

The id is the provider id the service uses for detected providers. A guide's
file name is its id.

| Id | Provider |
| --- | --- |
| [`cloudflare`](cloudflare.md) | Cloudflare (the CNAME must be "DNS only") |
| [`godaddy`](godaddy.md) | GoDaddy |
| [`namecheap`](namecheap.md) | Namecheap |
| [`route53`](route53.md) | Amazon Route 53 |
| [`google-cloud-dns`](google-cloud-dns.md) | Google Cloud DNS |
| [`squarespace`](squarespace.md) | Squarespace Domains |
| [`porkbun`](porkbun.md) | Porkbun |
| [`hostinger`](hostinger.md) | Hostinger |
| [`ionos`](ionos.md) | IONOS |
| [`digitalocean`](digitalocean.md) | DigitalOcean |
| [`vercel`](vercel.md) | Vercel |
| [`netlify`](netlify.md) | Netlify |
| [`generic`](generic.md) | Any other provider |

## How the guides are checked

Each guide gives the provider's menu names, how it wants the record name
typed, whether TXT values take quotes, and how long changes usually take.
They were checked against the provider's own documentation on the date at
the end of the guide, and the sources are listed there. Provider consoles
change; when a guide and the console disagree, follow the console.

## Adding a guide

Name the file after the provider id, follow the structure of an existing
guide, cite only the provider's own documentation, add the date checked, and
add a row to the table above.
