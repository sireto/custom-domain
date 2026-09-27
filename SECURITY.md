# Security policy

Custom Domain terminates TLS for other people's hostnames and tells
applications which tenant a request belongs to, so we treat security reports
as the highest priority.

## Reporting a vulnerability

Report it privately through GitHub:
**[Report a vulnerability](https://github.com/sireto/custom-domain/security/advisories/new)**
(the "Security" tab, then "Report a vulnerability"). If you cannot use GitHub,
email info@sireto.com with "security" in the subject.

Please include what an attacker can do, the steps or a proof of concept, and
the version affected. Do not open a public issue or pull request for it.

We acknowledge reports within three working days, keep you updated while we
fix it, and credit you in the advisory unless you prefer otherwise.

## Supported versions

Fixes are released for the latest minor version. Upgrade with
`custom-domain upgrade <version>`.

| Version | Supported |
|---|---|
| 0.5.x | yes |
| older | no |

## In scope

Anything that breaks the guarantees in AGENTS.md ("Rules the code depends
on"), for example: serving or certifying a hostname that has not passed every
check; a request reaching an application with another tenant's workspace;
one application reading or changing another's objects; bypassing the edge
configuration gateway, the portal's sign-in, CSRF protection or IP allowlist;
or outbound requests (origin verification, webhooks) reaching private
addresses.
