# Sample SaaS origin

A second, minimal application that proves the integration is reusable: it
selects its workspace from the edge assertion using the SDK middleware,
answers the workspace probe, and serves its origin verification token. Two
workspaces exist, `ws_alpha` ("Alpha Forms") and `ws_beta` ("Beta Surveys").

## Run it locally

The local Compose environment starts this app next to the service and the
`dev demo` command walks it through onboarding (see the README, "Try it
locally"):

```
docker compose -f deploy/compose.local.yml up -d --build
docker compose -f deploy/compose.local.yml exec custom-domain custom-domain dev demo
```

Then open https://alpha.sample.localtest.me/ and https://beta.sample.localtest.me/.

## Run it by hand

The app reads three variables: `EDGE_ASSERTION_KEYS` (the same value the
service uses), `APPLICATION_ID` (from `custom-domain application create`) and
`ORIGIN_VERIFICATION_TOKEN` (from `custom-domain origin register`).

```
EDGE_ASSERTION_KEYS="1:<secret>" APPLICATION_ID=<uuid> ORIGIN_VERIFICATION_TOKEN=<token> \
uvicorn examples.sample_saas.app:app --port 8000
```

Register it as the application's origin (`custom-domain origin register`,
then `origin verify --activate`), issue a credential, and register workspace
domains with the SDK:

```python
from custom_domain import Client

client = Client("http://127.0.0.1:9000", credential="cd_...")
domain = client.create_domain("alpha.sample.localtest.me", "ws_alpha")
print(domain.dns_records)
```

Against a real deployment the customer publishes those records; against the
local environment `DNS_VERIFICATION_MODE=local` answers them from the
service's own data. Everything here uses invented hostnames and secrets.
