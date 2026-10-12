# One-step DNS setup with Domain Connect

[Domain Connect](https://github.com/Domain-Connect/spec) is an open standard
that lets a customer approve a service's DNS records at their own DNS
provider in one step, instead of typing them. The service never holds the
customer's DNS credentials: the provider shows the records and applies them
when the customer approves.

This deployment supports the standard's synchronous flow with signed
requests. When the DNS provider of a domain's zone supports the
deployment's template **and** the deployment can sign requests, the domain's
`setup.method` is `domain_connect` ([api-v1.md](api-v1.md#setup)) and the
application can ask for a signed apply URL:

```bash
curl -X POST https://edge.example.net/v1/domains/$DOMAIN_ID/connect \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"redirect_uri": "https://app.example/settings/domains?done=1", "state": "s-42"}'
# {"url": "https://dash.cloudflare.com/domainconnect/v2/domainTemplates/providers/...&key=_dcpubkeyv1&sig=...",
#  "provider": {"id": "cloudflare", "name": "Cloudflare"}, "expires_at": "..."}
```

The application sends the customer to `url`. Otherwise the request answers
`409 domain_connect_unavailable`, and the customer follows `setup.guide_url`.

Domain Connect is **off** unless the operator configures all of this:

1. a template, published in the Domain Connect templates repository and
   onboarded by the DNS providers;
2. a key pair, with the public key in DNS;
3. a signer: this deployment's own key (`local`), or a signing service
   (`remote`).

## 1. The template

The template describes the two records the service issues for each domain,
relative to the customer's host:

| Record | Host | Value |
| --- | --- | --- |
| `CNAME` | `@` | `%target%` (the application's CNAME target, fully qualified) |
| `TXT` | `_custom-domain-challenge` | `custom-domain-verify=%token%` |

Set `hostRequired: true` (the service never configures a zone apex),
`syncPubKeyDomain` to the domain where you publish the public key, and
`txtConflictMatchingMode` on the TXT record so a re-registration replaces
an old challenge. Leave `warnPhishing` unset: the template rules forbid it
together with signing. No `syncRedirectDomain` is needed, because every
request is signed and each application's return address is different.

Validate the template with the Domain Connect linter and the online editor,
then open a pull request to
[Domain-Connect/Templates](https://github.com/Domain-Connect/Templates).
Each DNS provider then onboards it on its own schedule; until a provider
has, its customers get `guided` instead.

Tell the deployment the template's identifiers:

```bash
DOMAIN_CONNECT_PROVIDER_ID=example.com   # the template's providerId
DOMAIN_CONNECT_SERVICE_ID=hostname       # its serviceId (default: hostname)
```

The DNS worker then checks, per zone, whether the provider supports that
template (cached 24 hours).

## 2. The key

Generate an RSA key of at least 2048 bits, offline, and keep an escrow copy:

```bash
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out dc.pem
openssl pkcs8 -topk8 -nocrypt -in dc.pem -outform DER | base64 -w0 > dc.key
```

`dc.key` is one line: the base64 of the key's PKCS#8 DER. Publish the public
half as TXT records at `<key host>.<syncPubKeyDomain>` (for example
`_dcpubkeyv1.example.com`), in the standard's format: `p=1,a=RS256,d=<first
200 characters>`, `p=2,a=RS256,d=<next 200>`, and so on, over the base64 of
the public key's SubjectPublicKeyInfo. With the key configured as a local
signer (below), the service prints the records for you:

```bash
custom-domain domain-connect public-key
# _dcpubkeyv1.<the template's syncPubKeyDomain>
# p=1,a=RS256,d=MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA...
# p=2,a=RS256,d=...
```

Check it with the Domain Connect example service's signature checker before
you rely on it.

**Rotation.** Publish the new key at a new host (`_dcpubkeyv2`), then list
both keys, newest first, as `<key host>:<base64>` entries separated by
commas, with `DOMAIN_CONNECT_KEY_HOST` unset:

```bash
DOMAIN_CONNECT_PRIVATE_KEY=_dcpubkeyv2:MIIE...,_dcpubkeyv1:MIIE...
```

The first key signs (its host is sent as `key`). Remove the old one, and its
TXT records, once no link signed with it can still be in use (an hour).

## 3. The signer

`DOMAIN_CONNECT_SIGNER` chooses who signs:

| Value | Signs with | Settings |
| --- | --- | --- |
| unset or `none` | nothing: Domain Connect is off, `setup.method` is never `domain_connect` | |
| `local` | the key above, in this deployment's processes | `DOMAIN_CONNECT_PRIVATE_KEY` (or `DOMAIN_CONNECT_PRIVATE_KEY_FILE`, a file holding the same value), `DOMAIN_CONNECT_KEY_HOST` (default `_dcpubkeyv1`; leave it unset with several keys) |
| `remote` | a signing service that holds the key | `DOMAIN_CONNECT_SIGNER_URL` (https), `DOMAIN_CONNECT_SIGNER_CREDENTIAL` (`dcs_` and 43 letters, digits, `-` or `_`) |

A value that does not parse (a key that is not an RSA PKCS#8 key of at least
2048 bits, an http URL, a malformed credential) stops the service at start.
The key and the credential are never logged, shown or returned.

### `local`: the trade-off

With `local`, the private key is in the deployment's `.env` (or the file),
and therefore in every container that reads it, including the API process
that serves the public v1 API. Whoever takes over that process, or reads
`.env`, can sign apply URLs for your template, for any domain, until you
rotate the key. What bounds the damage: a signed URL only proposes your two
records, and the customer must still approve them at their own provider.

That is a reasonable choice for a deployment that serves its own
applications. A deployment that serves many parties, or wants the key kept
off machines that face the internet, should use `remote`: the key stays in
a separate signing service that checks every request.

### `remote`: the signing contract

The service asks the signer for each link:

```http
POST <DOMAIN_CONNECT_SIGNER_URL>
Authorization: Bearer dcs_...
Content-Type: application/json

{"provider_id": "example.com", "service_id": "hostname", "application": "acme",
 "domain_id": "8a0c5f53-...", "query": "domain=customer.example&host=forms&target=edge.example.net&token=...&redirect_uri=...&state=..."}
```

- `query` is the apply URL's query string exactly as it will be sent,
  **without** `key` and `sig`: `domain`, `host`, `target` and `token`, then
  `redirect_uri` and `state` when present, each value percent-encoded as
  RFC 3986 does it (`urllib.parse.urlencode(params, quote_via=quote)`:
  letters, digits and `-._~` as they are, everything else `%XX`, spaces as
  `%20`).
- `200 {"sig": "<base64>", "key": "_dcpubkeyv1"}`: `sig` is RS256 over the
  query's bytes, `key` the TXT host of the public key. The service appends
  `&key=<key>&sig=<sig, percent-encoded>`, `sig` last.
- Refusals are `{"error": {"code", "message"}}`. A `400`, `401` or `403`
  (`bad_request`, `bad_query`, `unauthorized`, `foreign_template`,
  `redirect_not_https`, `wrong_target`, `unknown_domain`, `host_mismatch`,
  `domain_not_live`, `wrong_token`) reaches the application as
  `409 domain_connect_unavailable` with the code in `details.reason`; a
  `429` as `429 rate_limited` with the signer's `Retry-After`; anything
  else, a timeout (15 seconds) or an unusable answer as
  `503 domain_connect_signer_unavailable`. Redirects are not followed.
- The signer may check the domain by calling this deployment's
  `GET /v1/domains/{domain_id}` with a key of its own for that application
  while it answers: the service holds no lock or transaction while it waits.

Instead of the settings, the remote signer can be set at run time, through
the operator API (`PUT /operator/v1/domain-connect/signer`,
[operator-api.md](operator-api.md#domain-connect-signer)), the command
(`custom-domain domain-connect set-remote`, which reads the credential from
standard input) or the portal (**Edge & DNS**). That configuration, the
template ids included, overrides the `DOMAIN_CONNECT_SIGNER*` settings until
it is removed (`custom-domain domain-connect reset`); every API instance uses
it from the next request. Its credential is stored encrypted with
`SERVICE_SECRET_KEY` (AES-256-GCM, a key derived from it), so a database
dump or backup alone does not reveal it. `SERVICE_SECRET_KEY` is a random
value of at least 32 characters in `.env`; the installer generates it. If it
changes, the stored credential can no longer be read: `GET` then shows
`credential_prefix: null` and the signer must be set again.

## The return and the recheck

After the customer approves (or declines), the provider sends the browser to
the `redirect_uri` in the URL with its `state`. The service never trusts that
redirect: only its own DNS checks decide whether the records are in place,
and the result arrives as usual (`GET /v1/domains/{id}` or the
`domain.ready` webhook).

- **Fast recheck.** For 30 minutes after a link is issued, failing DNS checks
  run every 30 seconds instead of backing off.
- **The return address.** When the service has a public address, the URL
  sends the customer back to the service first:
  `https://<EDGE_HOSTNAME>/v1/domain-connect/return` when `PUBLIC_API=true`,
  or `DOMAIN_CONNECT_RETURN_URL` (the same path on your own proxy). The
  `state` in the URL is then the service's own: it names the domain, the
  application's `redirect_uri` and `state` and an expiry (an hour), and is
  protected by an HMAC keyed from `SERVICE_SECRET_KEY`, so only this service
  can make one. The return makes the domain's DNS checks due at once and
  restarts the fast recheck, then redirects to the application's
  `redirect_uri` with the application's `state` (and `error=access_denied` if
  the customer declined), or shows a short page when there is none. A
  forged or expired state gets an error page and changes nothing. The return
  is not an open redirect: it only goes where the sealed state says.
- **Without a return address** (no public API and no
  `DOMAIN_CONNECT_RETURN_URL`), or without `SERVICE_SECRET_KEY`, the
  application's `redirect_uri` and `state` go to the provider as given, and
  the fast recheck catches the change. An application that handles the
  return itself and wants a check at once calls
  `POST /v1/domains/{id}/checks`.

`redirect_uri` must be `https` with a host, no user name and no fragment, and
at most 512 bytes; `state` at most 512 bytes without control characters. Both
end up in a signed URL, so they are the application's responsibility: point
`redirect_uri` at a page of your own.

## Limits and audit

At most 10 links per domain and 120 per application per hour
(`429 rate_limited`); a signing service may apply its own. Each issued link
is recorded as a `domain.connect_url_issued` event on the domain (the
provider and the signer kind, never the URL or its signature) and logged by
`app.domain_connect.audit` with the application and the domain id. The
signature, the challenge token and the state are masked if they ever appear
in a log line.
